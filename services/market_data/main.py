"""FastAPI application that aggregates market data snapshots from Redis Pub/Sub."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Iterable, Mapping, Sequence

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from src.ibbot.influx import build_service_metric_point, write_points
from src.ibbot.logging import get_logger
from src.ibbot.redis import PubSubListener, get_redis_client

PRICE_CHANNEL_TEMPLATE = "pubsub:ib:price:{symbol}"
DOM_CHANNEL_TEMPLATE = "pubsub:ib:dom:{symbol}"
STREAM_KEY = "stream:market-service"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None
    return None


async def _maybe_await(result: Awaitable[Any] | Any) -> Any:
    if inspect.isawaitable(result):
        return await result  # type: ignore[return-value]
    return result


class WebSocketManager:
    """Manage active WebSocket connections and broadcast updates."""

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


@dataclass(slots=True)
class MarketDataRuntime:
    """Aggregate market snapshots and expose them over HTTP/WebSocket."""

    redis_factory: Callable[[], Awaitable[Any]] = get_redis_client
    write_fn: Callable[[Iterable[Any]], Awaitable[int] | int] = write_points
    pubsub_listener: PubSubListener | None = None
    subscriptions: Sequence[str] = ()
    logger_name: str = "ibbot.services.market_data"
    stream_key: str = STREAM_KEY
    metrics_interval: float = 5.0
    service_name: str = "market-data-service"
    stream_maxlen: int = 5000

    _loop: asyncio.AbstractEventLoop | None = field(default=None, init=False, repr=False)
    _redis: Any = field(default=None, init=False, repr=False)
    _pubsub: PubSubListener | None = field(default=None, init=False, repr=False)
    _ws_manager: WebSocketManager = field(default_factory=WebSocketManager, init=False, repr=False)
    _snapshots: Dict[str, Dict[str, Any]] = field(default_factory=dict, init=False, repr=False)
    _updated_at: Dict[str, datetime] = field(default_factory=dict, init=False, repr=False)
    _metrics_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _stop_metrics: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _metrics_counts: Dict[str, float] = field(default_factory=lambda: defaultdict(float), init=False, repr=False)
    _metrics_latency: Dict[str, float] = field(default_factory=lambda: defaultdict(float), init=False, repr=False)
    _metrics_latency_max: Dict[str, float] = field(default_factory=lambda: defaultdict(float), init=False, repr=False)
    _last_metrics_at: datetime = field(default_factory=_now, init=False, repr=False)
    _logger: Any = field(default=None, init=False, repr=False)

    async def start(self) -> None:
        self._logger = get_logger(self.logger_name)
        self._loop = asyncio.get_running_loop()
        self._redis = await self.redis_factory()
        self._pubsub = self.pubsub_listener or PubSubListener(redis_factory=self.redis_factory)
        await self._register_channels()
        await self._pubsub.start()
        self._stop_metrics.clear()
        self._last_metrics_at = _now()
        self._metrics_task = asyncio.create_task(self._metrics_loop(), name="market-metrics")

    async def stop(self) -> None:
        if self._metrics_task is not None:
            self._stop_metrics.set()
            self._metrics_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._metrics_task
            self._metrics_task = None
        if self._pubsub is not None:
            await self._pubsub.stop()
            self._pubsub = None
        self._redis = None
        self._loop = None

    async def _register_channels(self) -> None:
        if self._pubsub is None:
            return
        for symbol in self.subscriptions:
            price_channel = PRICE_CHANNEL_TEMPLATE.format(symbol=symbol)
            dom_channel = DOM_CHANNEL_TEMPLATE.format(symbol=symbol)
            self._pubsub.register_callback(price_channel, self._handle_price_message)
            self._pubsub.register_callback(dom_channel, self._handle_dom_message)

    async def _handle_price_message(self, channel: str, payload: str) -> None:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            self._logger.warning("Invalid price payload", channel=channel)
            return
        symbol = str(data.get("symbol")) if data.get("symbol") else None
        if not symbol:
            self._logger.warning("Price payload missing symbol", channel=channel)
            return
        timestamp = _parse_timestamp(data.get("timestamp")) or _now()
        await self._update_snapshot(symbol, {"price": data}, timestamp, message_type="price")

    async def _handle_dom_message(self, channel: str, payload: str) -> None:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            self._logger.warning("Invalid DOM payload", channel=channel)
            return
        symbol = str(data.get("symbol")) if data.get("symbol") else None
        if not symbol:
            self._logger.warning("DOM payload missing symbol", channel=channel)
            return
        timestamp = _parse_timestamp(data.get("timestamp")) or _now()
        await self._update_snapshot(symbol, {"dom": data}, timestamp, message_type="dom")

    async def _update_snapshot(
        self,
        symbol: str,
        components: Mapping[str, Any],
        timestamp: datetime,
        *,
        message_type: str,
    ) -> None:
        existing = self._snapshots.setdefault(symbol, {"symbol": symbol, "price": None, "dom": None})
        existing.update({key: value for key, value in components.items()})
        self._updated_at[symbol] = timestamp
        latency = max((_now() - timestamp).total_seconds(), 0.0)
        await self._append_stream(symbol, message_type, components)
        await self._broadcast(symbol, message_type)
        self._record_metrics(message_type, latency)

    async def _append_stream(self, symbol: str, message_type: str, components: Mapping[str, Any]) -> None:
        if self._redis is None:
            return
        payload = json.dumps({"symbol": symbol, "type": message_type, **components}, separators=(",", ":"))
        fields = {
            "symbol": symbol,
            "type": message_type,
            "payload": payload,
        }
        try:
            await self._redis.xadd(
                self.stream_key,
                fields,
                maxlen=self.stream_maxlen,
                approximate=True,
            )
        except Exception:
            self._logger.warning("Failed to append to market stream", symbol=symbol, type=message_type, exc_info=True)

    async def _broadcast(self, symbol: str, message_type: str) -> None:
        snapshot = self.get_snapshot(symbol)
        if snapshot is None:
            return
        payload = {"symbol": symbol, "type": message_type, "data": snapshot}
        await self._ws_manager.broadcast(payload)

    def _record_metrics(self, message_type: str, latency: float) -> None:
        self._metrics_counts[message_type] += 1
        self._metrics_latency[message_type] += latency
        self._metrics_latency_max[message_type] = max(self._metrics_latency_max[message_type], latency)

    async def _metrics_loop(self) -> None:
        try:
            while True:
                try:
                    await asyncio.wait_for(self._stop_metrics.wait(), timeout=self.metrics_interval)
                    break
                except asyncio.TimeoutError:
                    await self._flush_metrics()
        finally:
            await self._flush_metrics(final=True)

    async def _flush_metrics(self, *, final: bool = False) -> None:
        now = _now()
        elapsed = (now - self._last_metrics_at).total_seconds()
        if elapsed <= 0:
            elapsed = self.metrics_interval
        self._last_metrics_at = now
        points = []
        for metric, count in list(self._metrics_counts.items()):
            if count <= 0 and not final:
                continue
            latency_total = self._metrics_latency.get(metric, 0.0)
            max_latency = self._metrics_latency_max.get(metric, 0.0)
            avg_latency = latency_total / count if count else 0.0
            throughput = count / elapsed if elapsed > 0 else 0.0
            points.append(
                build_service_metric_point(
                    service=self.service_name,
                    metric=f"{metric}-throughput",
                    value=throughput,
                    extra_fields={"updates": count, "interval": elapsed},
                )
            )
            points.append(
                build_service_metric_point(
                    service=self.service_name,
                    metric=f"{metric}-latency",
                    value=avg_latency,
                    extra_fields={"max_latency": max_latency, "updates": count},
                )
            )
            self._metrics_counts[metric] = 0.0
            self._metrics_latency[metric] = 0.0
            self._metrics_latency_max[metric] = 0.0
        if points:
            try:
                await _maybe_await(self.write_fn(points))
            except Exception:
                self._logger.warning("Failed to write service metrics", exc_info=True)

    def get_snapshot(self, symbol: str) -> Dict[str, Any] | None:
        symbol = symbol.upper()
        if symbol not in self._snapshots:
            return None
        snapshot = dict(self._snapshots[symbol])
        updated = self._updated_at.get(symbol)
        snapshot["symbol"] = symbol
        snapshot["updated_at"] = updated.isoformat() if updated else None
        return snapshot

    def get_all_snapshots(self) -> list[Dict[str, Any]]:
        result: list[Dict[str, Any]] = []
        for symbol in sorted(self._snapshots.keys()):
            entry = self.get_snapshot(symbol)
            if entry is not None:
                result.append(entry)
        return result

    def emit_price(self, symbol: str, data: Mapping[str, Any]) -> Any:
        payload = json.dumps({**data, "symbol": symbol}, separators=(",", ":"))
        channel = PRICE_CHANNEL_TEMPLATE.format(symbol=symbol)
        return self._schedule(self._handle_price_message(channel, payload))

    def emit_dom(self, symbol: str, data: Mapping[str, Any]) -> Any:
        payload = json.dumps({**data, "symbol": symbol}, separators=(",", ":"))
        channel = DOM_CHANNEL_TEMPLATE.format(symbol=symbol)
        return self._schedule(self._handle_dom_message(channel, payload))

    def _schedule(self, coro: Awaitable[Any]) -> Any:
        loop = self._loop
        if loop is None:
            raise RuntimeError("Service is not running")
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            return asyncio.create_task(coro)
        return asyncio.run_coroutine_threadsafe(coro, loop)


def create_app(*, runtime: MarketDataRuntime | None = None) -> FastAPI:
    runtime = runtime or MarketDataRuntime()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(title="Market Data Service", lifespan=lifespan)
    app.state.market_runtime = runtime

    @app.get("/market/{symbol}")
    async def get_market(symbol: str) -> JSONResponse:
        snapshot = runtime.get_snapshot(symbol)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="Symbol not found")
        return JSONResponse(snapshot)

    @app.websocket("/ws/market")
    async def websocket_market(websocket: WebSocket) -> None:
        await runtime._ws_manager.connect(
            websocket,
            {"type": "snapshot", "data": runtime.get_all_snapshots()},
        )
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            await runtime._ws_manager.disconnect(websocket)

    return app


app = create_app()
