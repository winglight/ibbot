from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Mapping, MutableMapping

from services.risk.engine import RiskEngine, RiskEvent
from src.ibbot.logging import get_logger
from src.ibbot.redis import (
    ack_stream,
    enqueue_ib_command,
    read_stream,
    ResponseError,
)
from src.ibbot.influx import write_points

try:  # Optional dependency during testing environments.
    from redis.asyncio import Redis
except Exception:  # pragma: no cover - redis might be unavailable in CI
    Redis = Any  # type: ignore[misc, assignment]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def _maybe_await(result: Awaitable[Any] | Any) -> Any:
    if inspect.isawaitable(result):
        return await result  # type: ignore[return-value]
    return result


@dataclass(slots=True)
class RiskRuntime:
    """Orchestrate the risk engine and connect it to external services."""

    redis_factory: Callable[[], Awaitable[Redis]]
    write_fn: Callable[[Iterable[Mapping[str, Any]]], Awaitable[int] | int] = write_points
    logger_name: str = "risk-service"
    service_name: str = "risk-service"
    market_stream_key: str = "stream:market-service"
    market_group: str = "group:risk:market"
    results_stream_key: str = "stream:ib:results"
    results_group: str = "group:risk:results"
    command_queue: str = "queue:ib:commands"
    poll_interval: float = 0.5
    log_dir: str | os.PathLike[str] | None = None
    batch_size: int = 20
    reconnect_delay: float = 1.0

    _engine: RiskEngine = field(default_factory=RiskEngine, init=False, repr=False)
    _redis: Redis | None = field(default=None, init=False, repr=False)
    _logger: Any = field(default=None, init=False, repr=False)
    _market_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _results_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _consumer_name: str = field(default_factory=lambda: f"risk-{uuid.uuid4().hex[:8]}", init=False, repr=False)
    _pending_commands: MutableMapping[str, RiskEvent] = field(default_factory=dict, init=False, repr=False)
    _configured: bool = field(default=False, init=False, repr=False)
    _config_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    async def update_config(self, config: Mapping[str, Any]) -> None:
        async with self._config_lock:
            self._engine.load_config(config)
            self.command_queue = str(config.get("command_queue", self.command_queue))
            self.market_stream_key = str(config.get("market_stream", self.market_stream_key))
            self.market_group = str(config.get("market_group", self.market_group))
            self.results_stream_key = str(config.get("results_stream", self.results_stream_key))
            self.results_group = str(config.get("results_group", self.results_group))
            poll_value = config.get("poll_interval", self.poll_interval)
            try:
                self.poll_interval = max(0.05, float(poll_value))
            except (TypeError, ValueError):
                pass
            batch_value = config.get("batch_size", self.batch_size)
            try:
                batch = int(batch_value)
                if batch > 0:
                    self.batch_size = batch
            except (TypeError, ValueError):
                pass
            self._configured = True

    async def start(self) -> None:
        if not self._configured:
            raise RuntimeError("RiskRuntime must be configured before starting")
        self._logger = get_logger(self.logger_name, log_dir=self.log_dir)
        self._redis = await self.redis_factory()
        await self._ensure_stream_groups()
        self._stop_event.clear()
        self._market_task = asyncio.create_task(self._market_loop(), name="risk-market-loop")
        self._results_task = asyncio.create_task(self._results_loop(), name="risk-results-loop")

    async def stop(self) -> None:
        self._stop_event.set()
        for task in (self._market_task, self._results_task):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(self._await_task(task) for task in (self._market_task, self._results_task)),
            return_exceptions=True,
        )
        self._market_task = None
        self._results_task = None
        self._redis = None
        self._pending_commands.clear()

    async def _await_task(self, task: asyncio.Task[None] | None) -> None:
        if task is None:
            return
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _ensure_stream_groups(self) -> None:
        if self._redis is None:
            return
        for stream, group in (
            (self.market_stream_key, self.market_group),
            (self.results_stream_key, self.results_group),
        ):
            try:
                await self._redis.xgroup_create(stream, group, id="$", mkstream=True)
            except ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise
            except Exception:
                if self._logger:
                    self._logger.warning(
                        "Failed to create consumer group for %s/%s", stream, group, exc_info=True
                    )

    async def _market_loop(self) -> None:
        assert self._redis is not None
        while not self._stop_event.is_set():
            try:
                messages = await read_stream(
                    stream_key=self.market_stream_key,
                    group=self.market_group,
                    consumer=self._consumer_name,
                    count=self.batch_size,
                    block=int(self.poll_interval * 1000),
                    redis=self._redis,
                )
            except Exception:
                if self._logger:
                    self._logger.warning("Failed to read market stream", exc_info=True)
                await asyncio.sleep(self.reconnect_delay)
                continue
            if not messages:
                continue
            ack_ids: list[str] = []
            for message in messages:
                handled = await self._handle_market_message(message.data)
                if handled:
                    ack_ids.append(message.message_id)
            if ack_ids:
                try:
                    await ack_stream(ack_ids, stream_key=self.market_stream_key, group=self.market_group, redis=self._redis)
                except Exception:
                    if self._logger:
                        self._logger.warning("Failed to acknowledge market messages", exc_info=True)

    async def _results_loop(self) -> None:
        assert self._redis is not None
        while not self._stop_event.is_set():
            try:
                messages = await read_stream(
                    stream_key=self.results_stream_key,
                    group=self.results_group,
                    consumer=f"{self._consumer_name}-results",
                    count=self.batch_size,
                    block=int(self.poll_interval * 1000),
                    redis=self._redis,
                )
            except Exception:
                if self._logger:
                    self._logger.warning("Failed to read results stream", exc_info=True)
                await asyncio.sleep(self.reconnect_delay)
                continue
            if not messages:
                continue
            ack_ids: list[str] = []
            for message in messages:
                handled = await self._handle_result_message(message.data)
                if handled:
                    ack_ids.append(message.message_id)
            if ack_ids:
                try:
                    await ack_stream(ack_ids, stream_key=self.results_stream_key, group=self.results_group, redis=self._redis)
                except Exception:
                    if self._logger:
                        self._logger.warning("Failed to acknowledge result messages", exc_info=True)

    async def _handle_market_message(self, payload: Mapping[str, Any]) -> bool:
        symbol = payload.get("symbol")
        if not symbol:
            return True
        body: Mapping[str, Any]
        raw = payload.get("payload")
        if isinstance(raw, str):
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = {}
        elif isinstance(raw, Mapping):
            body = raw
        else:
            body = payload
        price_value = body.get("last") or body.get("price") or body.get("close")
        try:
            price = float(price_value)
        except (TypeError, ValueError):
            return True
        events = self._engine.evaluate_price(str(symbol), price, body)
        for event in events:
            await self._dispatch_event(event)
        return True

    async def _handle_result_message(self, payload: Mapping[str, Any]) -> bool:
        command_id = payload.get("command_id")
        if not command_id:
            return True
        command_id = str(command_id)
        event = self._pending_commands.pop(command_id, None)
        if not event:
            return True
        status = str(payload.get("status") or "").lower()
        self._engine.reset_trigger(event.symbol, event.trigger)
        message = f"Received result for {event.symbol}: {status or 'unknown'}"
        if self._logger:
            self._logger.info(message)
        await self._write_log_point(
            level="INFO",
            message=message,
            symbol=event.symbol,
            trigger=event.trigger,
            status=status or "unknown",
        )
        return True

    async def _dispatch_event(self, event: RiskEvent) -> None:
        command_id = uuid.uuid4().hex
        event.command_id = command_id
        payload = event.build_command(command_id=command_id)
        try:
            await enqueue_ib_command(payload, redis=self._redis, queue_key=self.command_queue)
        except Exception:
            self._engine.reset_trigger(event.symbol, event.trigger)
            if self._logger:
                self._logger.error("Failed to enqueue risk command", extra={"symbol": event.symbol})
            return
        self._pending_commands[command_id] = event
        message = f"Triggered {event.trigger} for {event.symbol} at {event.price:.4f}"
        if self._logger:
            self._logger.warning(message)
        await self._write_log_point(
            level="WARN",
            message=message,
            symbol=event.symbol,
            trigger=event.trigger,
            price=float(event.price),
        )

    async def _write_log_point(self, **fields: Any) -> None:
        timestamp = _utc_now()
        level = str(fields.pop("level", "INFO"))
        message = str(fields.pop("message", ""))
        point = {
            "measurement": "logs",
            "tags": {
                "service": self.service_name,
                "level": level,
            },
            "fields": {
                "message": message,
                **{k: v for k, v in fields.items() if isinstance(v, (int, float, str))},
            },
            "time": timestamp,
        }
        try:
            await _maybe_await(self.write_fn([point]))
        except Exception:
            if self._logger:
                self._logger.debug("Failed to write log point to Influx", exc_info=True)
