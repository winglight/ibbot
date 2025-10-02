"""Runtime for monitoring Redis queues and streams for operational metrics."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Iterable, Mapping, MutableMapping, Sequence

from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from src.ibbot import update_service_health
from src.ibbot.influx import build_service_metric_point, write_points
from src.ibbot.logging import get_logger
from src.ibbot.redis import (
    DEFAULT_COMMAND_QUEUE,
    get_queue_length,
    get_stream_metrics,
    get_redis_client,
)


Logger = logging.Logger


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _maybe_await(result: Awaitable[Any] | Any) -> Any:
    if inspect.isawaitable(result):
        return await result  # type: ignore[return-value]
    return result


@dataclass(slots=True)
class QueueTarget:
    """Configuration describing a Redis list queue to monitor."""

    name: str
    queue_key: str


@dataclass(slots=True)
class StreamTarget:
    """Configuration describing a Redis stream consumer group to monitor."""

    name: str
    stream_key: str
    group: str


@dataclass(slots=True)
class _QueueState:
    length: int
    timestamp: datetime
    nonempty_since: datetime | None


@dataclass(slots=True)
class _StreamState:
    pending: int
    timestamp: datetime
    nonempty_since: datetime | None


class WebSocketManager:
    """Manage WebSocket clients and broadcast updates."""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, initial_payload: Mapping[str, Any]) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)
        await websocket.send_json(initial_payload)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(websocket)

    async def broadcast(self, payload: Mapping[str, Any]) -> None:
        async with self._lock:
            connections = list(self._connections)
        for websocket in connections:
            try:
                await websocket.send_json(payload)
            except WebSocketDisconnect:
                await self.disconnect(websocket)
            except RuntimeError:
                await self.disconnect(websocket)


def _normalise_queue_targets(targets: Sequence[QueueTarget | Mapping[str, Any] | str]) -> tuple[QueueTarget, ...]:
    result: list[QueueTarget] = []
    for entry in targets:
        if isinstance(entry, QueueTarget):
            result.append(entry)
            continue
        if isinstance(entry, Mapping):
            queue_key = str(entry["queue_key"])
            name = str(entry.get("name") or queue_key)
            result.append(QueueTarget(name=name, queue_key=queue_key))
            continue
        queue_key = str(entry)
        name = queue_key.split(":")[-1] if ":" in queue_key else queue_key
        result.append(QueueTarget(name=name, queue_key=queue_key))
    return tuple(result)


def _normalise_stream_targets(
    targets: Sequence[StreamTarget | Mapping[str, Any] | Sequence[str]],
) -> tuple[StreamTarget, ...]:
    result: list[StreamTarget] = []
    for entry in targets:
        if isinstance(entry, StreamTarget):
            result.append(entry)
            continue
        if isinstance(entry, Mapping):
            stream_key = str(entry["stream_key"])
            group = str(entry["group"])
            name = str(entry.get("name") or stream_key)
            result.append(StreamTarget(name=name, stream_key=stream_key, group=group))
            continue
        if isinstance(entry, Sequence) and len(entry) >= 2:
            stream_key = str(entry[0])
            group = str(entry[1])
            name = str(entry[2]) if len(entry) > 2 else stream_key
            result.append(StreamTarget(name=name, stream_key=stream_key, group=group))
            continue
        raise TypeError(f"Unsupported stream target specification: {entry!r}")
    return tuple(result)


@dataclass(slots=True)
QUEUE_MONITOR_METRICS_HASH_KEY = os.getenv("QUEUE_MONITOR_METRICS_HASH_KEY", "hash:service:queue-monitor")
QUEUE_MONITOR_HEALTH_HASH_KEY = os.getenv("QUEUE_MONITOR_HEALTH_HASH_KEY", "hash:service:health:queue")
QUEUE_MONITOR_PUBSUB_CHANNEL = os.getenv("QUEUE_MONITOR_PUBSUB_CHANNEL", "pubsub:service:queue-monitor")
QUEUE_MONITOR_SERVICE_NAME = os.getenv("QUEUE_MONITOR_SERVICE_NAME", "queue-monitor")


class QueueMonitorRuntime:
    """Monitor Redis queue metrics and expose them for dashboards."""

    queues: Sequence[QueueTarget | Mapping[str, Any] | str] = field(
        default_factory=lambda: (QueueTarget(name="ib-commands", queue_key=DEFAULT_COMMAND_QUEUE),)
    )
    stream_targets: Sequence[StreamTarget | Mapping[str, Any] | Sequence[str]] = field(default_factory=tuple)
    redis_factory: Callable[[], Awaitable[Any]] = get_redis_client
    write_fn: Callable[[Iterable[Any]], Awaitable[int] | int] = write_points
    metrics_interval: float = 5.0
    hash_key: str = QUEUE_MONITOR_METRICS_HASH_KEY
    pubsub_channel: str | None = QUEUE_MONITOR_PUBSUB_CHANNEL or None
    service_name: str = QUEUE_MONITOR_SERVICE_NAME
    health_hash_key: str = QUEUE_MONITOR_HEALTH_HASH_KEY
    logger_name: str = "ibbot.services.queue_monitor"

    _queue_targets: tuple[QueueTarget, ...] = field(init=False, repr=False)
    _stream_targets: tuple[StreamTarget, ...] = field(init=False, repr=False)
    _queue_state: MutableMapping[str, _QueueState] = field(default_factory=dict, init=False, repr=False)
    _stream_state: MutableMapping[tuple[str, str], _StreamState] = field(
        default_factory=dict, init=False, repr=False
    )
    _snapshot: Dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _redis: Any = field(default=None, init=False, repr=False)
    _loop: asyncio.AbstractEventLoop | None = field(default=None, init=False, repr=False)
    _task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _ws_manager: WebSocketManager = field(default_factory=WebSocketManager, init=False, repr=False)
    _logger: Logger | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._queue_targets = _normalise_queue_targets(self.queues)
        self._stream_targets = _normalise_stream_targets(self.stream_targets)
        self._snapshot = self._empty_snapshot()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._logger = get_logger(self.logger_name)
        self._loop = asyncio.get_running_loop()
        self._redis = await self.redis_factory()
        self._stop_event.clear()
        try:
            await self.collect_once()
        except Exception:
            if self._logger:
                self._logger.warning("Initial queue metrics collection failed", exc_info=True)
        self._task = asyncio.create_task(self._metrics_loop(), name="queue-monitor-metrics")

    async def stop(self) -> None:
        self._stop_event.set()
        task = self._task
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._task = None
        self._loop = None
        self._redis = None

    def _empty_snapshot(self) -> Dict[str, Any]:
        return {"timestamp": _now().isoformat(), "queues": [], "streams": []}

    async def _metrics_loop(self) -> None:
        try:
            while True:
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.metrics_interval)
                    break
                except asyncio.TimeoutError:
                    await self.collect_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            if self._logger:
                self._logger.exception("Queue monitor loop crashed")
        finally:
            await self.collect_once()

    async def collect_once(self) -> Dict[str, Any]:
        if self._redis is None:
            self._redis = await self.redis_factory()
        now = _now()
        queue_metrics = await self._collect_queue_metrics(now)
        stream_metrics = await self._collect_stream_metrics(now)
        snapshot = {
            "timestamp": now.isoformat(),
            "queues": queue_metrics,
            "streams": stream_metrics,
        }
        self._snapshot = snapshot
        await self._write_snapshot(snapshot)
        await self._write_service_metrics(queue_metrics, stream_metrics)
        await self._publish_update(snapshot)
        await self._ws_manager.broadcast({"type": "metrics", "data": snapshot})
        await self._publish_health(queue_metrics, stream_metrics)
        return snapshot

    async def _publish_health(
        self,
        queue_metrics: Iterable[Mapping[str, Any]],
        stream_metrics: Iterable[Mapping[str, Any]],
    ) -> None:
        if self._redis is None:
            return
        total_queue_length = sum(int(entry.get("length", 0) or 0) for entry in queue_metrics)
        total_stream_pending = sum(int(entry.get("pending", 0) or 0) for entry in stream_metrics)
        metrics = {
            "total_queue_length": total_queue_length,
            "total_stream_pending": total_stream_pending,
        }
        await update_service_health(
            self._redis,
            self.service_name,
            healthy=True,
            metrics=metrics,
            key=self.health_hash_key,
            logger=self._logger,
        )

    async def _collect_queue_metrics(self, now: datetime) -> list[Dict[str, Any]]:
        metrics: list[Dict[str, Any]] = []
        for target in self._queue_targets:
            length = await get_queue_length(queue_key=target.queue_key, redis=self._redis)
            state = self._queue_state.get(target.queue_key)
            rate = 0.0
            nonempty_since: datetime | None = None
            if state is not None:
                elapsed = (now - state.timestamp).total_seconds()
                if elapsed > 0:
                    rate = max((state.length - length) / elapsed, 0.0)
                nonempty_since = state.nonempty_since
            if length > 0:
                if nonempty_since is None or (state is not None and state.length <= 0):
                    nonempty_since = now
            else:
                nonempty_since = None
            stale_seconds = (now - nonempty_since).total_seconds() if nonempty_since else 0.0
            self._queue_state[target.queue_key] = _QueueState(
                length=length, timestamp=now, nonempty_since=nonempty_since
            )
            metrics.append(
                {
                    "name": target.name,
                    "queue_key": target.queue_key,
                    "length": length,
                    "consumption_rate": rate,
                    "stale_seconds": stale_seconds,
                }
            )
        return metrics

    async def _collect_stream_metrics(self, now: datetime) -> list[Dict[str, Any]]:
        metrics: list[Dict[str, Any]] = []
        for target in self._stream_targets:
            details = await get_stream_metrics(
                stream_key=target.stream_key,
                group=target.group,
                redis=self._redis,
            )
            pending = int(details.get("pending", 0))
            stale_ms = int(details.get("stale_ms", 0))
            state_key = (target.stream_key, target.group)
            state = self._stream_state.get(state_key)
            rate = 0.0
            nonempty_since: datetime | None = None
            if state is not None:
                elapsed = (now - state.timestamp).total_seconds()
                if elapsed > 0:
                    rate = max((state.pending - pending) / elapsed, 0.0)
                nonempty_since = state.nonempty_since
            if pending > 0:
                if nonempty_since is None or (state is not None and state.pending <= 0):
                    nonempty_since = now
            else:
                nonempty_since = None
            stale_seconds = stale_ms / 1000.0 if stale_ms else 0.0
            if stale_seconds <= 0 and nonempty_since is not None:
                stale_seconds = (now - nonempty_since).total_seconds()
            self._stream_state[state_key] = _StreamState(
                pending=pending, timestamp=now, nonempty_since=nonempty_since
            )
            metrics.append(
                {
                    "name": target.name,
                    "stream_key": target.stream_key,
                    "group": target.group,
                    "pending": pending,
                    "consumption_rate": rate,
                    "stale_seconds": stale_seconds,
                }
            )
        return metrics

    async def _write_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        if self._redis is None:
            return
        mapping: dict[str, str] = {"timestamp": str(snapshot.get("timestamp", ""))}
        for entry in snapshot.get("queues", []):
            if isinstance(entry, Mapping):
                key = f"queue:{entry.get('name') or entry.get('queue_key')}"
                mapping[key] = json.dumps(entry, separators=(",", ":"))
        for entry in snapshot.get("streams", []):
            if isinstance(entry, Mapping):
                key = f"stream:{entry.get('name') or entry.get('stream_key')}"
                mapping[key] = json.dumps(entry, separators=(",", ":"))
        try:
            await self._redis.hset(self.hash_key, mapping=mapping)
        except Exception:
            if self._logger:
                self._logger.warning("Failed to persist queue metrics", exc_info=True)

    async def _write_service_metrics(
        self,
        queue_metrics: Iterable[Mapping[str, Any]],
        stream_metrics: Iterable[Mapping[str, Any]],
    ) -> None:
        points = []
        for entry in queue_metrics:
            slug = _slugify_metric(entry.get("name") or entry.get("queue_key", "queue"))
            points.append(
                build_service_metric_point(
                    service=self.service_name,
                    metric=f"{slug}-queue-length",
                    value=float(entry.get("length", 0)),
                    extra_fields={"queue_key": entry.get("queue_key")},
                )
            )
            points.append(
                build_service_metric_point(
                    service=self.service_name,
                    metric=f"{slug}-consumption-rate",
                    value=float(entry.get("consumption_rate", 0.0)),
                    extra_fields={"queue_key": entry.get("queue_key")},
                )
            )
            points.append(
                build_service_metric_point(
                    service=self.service_name,
                    metric=f"{slug}-stale-seconds",
                    value=float(entry.get("stale_seconds", 0.0)),
                    extra_fields={"queue_key": entry.get("queue_key")},
                )
            )
        for entry in stream_metrics:
            slug = _slugify_metric(entry.get("name") or entry.get("stream_key", "stream"))
            points.append(
                build_service_metric_point(
                    service=self.service_name,
                    metric=f"{slug}-pending",
                    value=float(entry.get("pending", 0)),
                    extra_fields={
                        "stream_key": entry.get("stream_key"),
                        "group": entry.get("group"),
                    },
                )
            )
            points.append(
                build_service_metric_point(
                    service=self.service_name,
                    metric=f"{slug}-consumption-rate",
                    value=float(entry.get("consumption_rate", 0.0)),
                    extra_fields={
                        "stream_key": entry.get("stream_key"),
                        "group": entry.get("group"),
                    },
                )
            )
            points.append(
                build_service_metric_point(
                    service=self.service_name,
                    metric=f"{slug}-stale-seconds",
                    value=float(entry.get("stale_seconds", 0.0)),
                    extra_fields={
                        "stream_key": entry.get("stream_key"),
                        "group": entry.get("group"),
                    },
                )
            )
        if not points:
            return
        try:
            await _maybe_await(self.write_fn(points))
        except Exception:
            if self._logger:
                self._logger.warning("Failed to write service metrics", exc_info=True)

    async def _publish_update(self, snapshot: Mapping[str, Any]) -> None:
        if not self.pubsub_channel or self._redis is None:
            return
        try:
            payload = json.dumps(snapshot, separators=(",", ":"))
            await self._redis.publish(self.pubsub_channel, payload)
        except Exception:
            if self._logger:
                self._logger.warning("Failed to publish queue metrics", exc_info=True)

    def get_snapshot(self) -> Dict[str, Any]:
        snapshot = dict(self._snapshot)
        queues = [dict(entry) for entry in self._snapshot.get("queues", []) if isinstance(entry, Mapping)]
        streams = [dict(entry) for entry in self._snapshot.get("streams", []) if isinstance(entry, Mapping)]
        snapshot["queues"] = queues
        snapshot["streams"] = streams
        return snapshot

    async def connect_websocket(self, websocket: WebSocket) -> None:
        snapshot = self.get_snapshot()
        await self._ws_manager.connect(websocket, {"type": "snapshot", "data": snapshot})

    async def disconnect_websocket(self, websocket: WebSocket) -> None:
        await self._ws_manager.disconnect(websocket)


def _slugify_metric(name: Any) -> str:
    text = str(name)
    return text.replace(" ", "-").replace(":", "-").replace("/", "-").lower()


def create_app(*, runtime: QueueMonitorRuntime | None = None) -> FastAPI:
    runtime = runtime or QueueMonitorRuntime()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(lifespan=lifespan)

    @app.get("/metrics")
    async def read_metrics() -> Mapping[str, Any]:
        return runtime.get_snapshot()

    @app.websocket("/ws/metrics")
    async def metrics_websocket(websocket: WebSocket) -> None:
        await runtime.connect_websocket(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            await runtime.disconnect_websocket(websocket)

    return app
