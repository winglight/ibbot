import argparse
import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Iterable, Mapping

from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel, ConfigDict

from src.ibbot import update_service_health
from src.ibbot.redis import DEFAULT_COMMAND_QUEUE, enqueue_ib_command, get_redis_client

DEFAULT_HOST = os.getenv("ORDERS_SERVICE_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.getenv("ORDERS_SERVICE_PORT", "8050"))

LOGGER = logging.getLogger("ibbot.services.orders")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _serialise_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.isoformat()


def _normalise_stream_payload(data: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(key, (bytes, bytearray)):
            key = key.decode("utf-8", errors="ignore")
        else:
            key = str(key)
        if isinstance(value, (bytes, bytearray)):
            try:
                payload[key] = value.decode("utf-8")
            except Exception:
                payload[key] = value
        else:
            payload[key] = value
    return payload


class OrderSubmission(BaseModel):
    symbol: str
    quantity: float
    action: str
    order_type: str = "MARKET"
    exchange: str | None = None
    sec_type: str | None = None
    account: str | None = None

    model_config = ConfigDict(extra="allow")


@dataclass
class OrdersRuntime:
    redis_factory: Callable[[], Awaitable[Any]] = get_redis_client
    command_queue: str = DEFAULT_COMMAND_QUEUE
    results_stream: str = "stream:ib:results"
    snapshot_key: str = "json:orders:snapshot"
    history_key: str = "list:orders:history"
    stream_poll_interval: float = 1.0
    health_interval: float = 15.0
    logger: logging.Logger = field(default_factory=lambda: LOGGER)

    _redis: Any = field(default=None, init=False, repr=False)
    _orders: Dict[str, Dict[str, Any]] = field(default_factory=dict, init=False, repr=False)
    _command_to_order: Dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _last_message_id: str | None = field(default=None, init=False, repr=False)
    _last_updated_at: datetime | None = field(default=None, init=False, repr=False)
    _stream_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _health_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    async def start(self) -> None:
        if self._stream_task is not None or self._redis is not None:
            return

        self._redis = await self.redis_factory()
        await self._load_snapshot()
        await self._publish_health()

        self._stop_event.clear()

        if self.stream_poll_interval > 0:
            self._stream_task = asyncio.create_task(self._stream_loop(), name="orders-stream")
        if self.health_interval > 0:
            self._health_task = asyncio.create_task(self._health_loop(), name="orders-health")

    async def stop(self) -> None:
        self._stop_event.set()
        for task in (self._stream_task, self._health_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._stream_task = None
        self._health_task = None
        self._redis = None

    async def submit_order(self, request: OrderSubmission) -> str:
        if self._redis is None:
            raise RuntimeError("Orders runtime not started")

        command_id = uuid.uuid4().hex
        payload = {
            "command_id": command_id,
            "method": "place_order",
            "args": [],
            "kwargs": request.model_dump(mode="json"),
        }
        await enqueue_ib_command(payload, redis=self._redis, queue_key=self.command_queue)

        entry = {
            "id": command_id,
            "command_id": command_id,
            "status": "pending",
            "requested_at": _serialise_datetime(_utc_now()),
            "request": request.model_dump(mode="json"),
        }
        async with self._lock:
            self._orders[command_id] = entry
            self._command_to_order[command_id] = command_id
            self._last_updated_at = _utc_now()
            await self._persist_snapshot()
            await self._append_history({"event": "submitted", "command_id": command_id, "order": entry})
        await self._publish_health()
        return command_id

    async def cancel_order(self, order_id: str) -> str:
        return await self._enqueue_simple_command("cancel_order", order_id=order_id)

    async def cancel_all(self) -> str:
        return await self._enqueue_simple_command("cancel_all_orders")

    async def close_position(self, symbol: str) -> str:
        return await self._enqueue_simple_command("close_position", symbol=symbol)

    async def reverse_position(self, symbol: str) -> str:
        return await self._enqueue_simple_command("reverse_position", symbol=symbol)

    async def _enqueue_simple_command(self, method: str, **kwargs: Any) -> str:
        if self._redis is None:
            raise RuntimeError("Orders runtime not started")
        command_id = uuid.uuid4().hex
        payload = {
            "command_id": command_id,
            "method": method,
            "args": [],
            "kwargs": dict(kwargs),
        }
        await enqueue_ib_command(payload, redis=self._redis, queue_key=self.command_queue)
        await self._append_history({"event": method, "command_id": command_id, "payload": kwargs})
        await self._publish_health()
        return command_id

    async def list_orders(self) -> dict[str, Any]:
        async with self._lock:
            orders = list(self._orders.values())
            updated_at = self._last_updated_at
        return {
            "orders": orders,
            "updated_at": _serialise_datetime(updated_at),
            "last_event_id": self._last_message_id or "",
        }

    async def poll_stream_once(self) -> int:
        if self._redis is None:
            return 0
        entries = await self._read_stream_entries()
        processed = 0
        for message_id, payload in entries:
            await self._handle_stream_entry(message_id, payload)
            processed += 1
        if processed:
            await self._publish_health()
        return processed

    async def _stream_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                processed = await self.poll_stream_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive guard
                self.logger.warning("Orders stream loop error: %s", exc)
                processed = 0
            if processed == 0:
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.stream_poll_interval)
                except asyncio.TimeoutError:
                    continue

    async def _health_loop(self) -> None:
        if self.health_interval <= 0:
            return
        while not self._stop_event.is_set():
            try:
                await self._publish_health()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive guard
                self.logger.warning("Orders health loop error: %s", exc)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.health_interval)
            except asyncio.TimeoutError:
                continue

    async def _load_snapshot(self) -> None:
        if self._redis is None:
            return
        try:
            raw = await self._redis.get(self.snapshot_key)
        except Exception:
            raw = None
        if not raw:
            return
        try:
            payload = json.loads(raw)
        except Exception:
            return
        orders = payload.get("orders")
        if isinstance(orders, Mapping):
            items = list(orders.values())
        elif isinstance(orders, Iterable):
            items = list(orders)
        else:
            items = []
        async with self._lock:
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                identifier = str(
                    item.get("id")
                    or item.get("order_id")
                    or item.get("command_id")
                    or item.get("ib_order_id")
                    or ""
                )
                if not identifier:
                    continue
                record = dict(item)
                record.setdefault("command_id", identifier)
                self._orders[identifier] = record
                if record.get("command_id"):
                    self._command_to_order[str(record["command_id"])] = identifier
            timestamp = payload.get("updated_at")
            if isinstance(timestamp, str) and timestamp:
                try:
                    self._last_updated_at = datetime.fromisoformat(timestamp)
                except Exception:
                    self._last_updated_at = None

    async def _persist_snapshot(self) -> None:
        if self._redis is None:
            return
        snapshot = {
            "orders": list(self._orders.values()),
            "updated_at": _serialise_datetime(self._last_updated_at),
        }
        try:
            await self._redis.set(self.snapshot_key, json.dumps(snapshot))
        except Exception as exc:  # pragma: no cover - defensive guard
            self.logger.debug("Failed to persist order snapshot: %s", exc)

    async def _append_history(self, entry: Mapping[str, Any]) -> None:
        if self._redis is None:
            return
        payload = dict(entry)
        payload.setdefault("timestamp", _serialise_datetime(_utc_now()))
        try:
            await self._redis.rpush(self.history_key, json.dumps(payload))
        except Exception as exc:  # pragma: no cover - defensive guard
            self.logger.debug("Failed to append order history: %s", exc)

    async def _publish_health(self) -> None:
        if self._redis is None:
            return
        async with self._lock:
            metrics = {
                "active_orders": len(self._orders),
                "last_event_id": self._last_message_id or "",
                "updated_at": _serialise_datetime(self._last_updated_at),
            }
        try:
            await update_service_health(
                self._redis,
                "orders",
                healthy=True,
                metrics=metrics,
                logger=self.logger,
            )
        except Exception as exc:  # pragma: no cover - defensive guard
            self.logger.debug("Failed to publish orders health: %s", exc)

    async def _read_stream_entries(self) -> list[tuple[str, Mapping[str, Any]]]:
        if self._redis is None:
            return []
        try:
            entries = await self._redis.xrange(self.results_stream)
        except Exception as exc:  # pragma: no cover - defensive guard
            self.logger.debug("Failed to read orders stream: %s", exc)
            return []
        new_entries: list[tuple[str, Mapping[str, Any]]] = []
        for message_id, payload in entries:
            if self._last_message_id and message_id <= self._last_message_id:
                continue
            if not isinstance(payload, Mapping):
                continue
            new_entries.append((message_id, payload))
        if new_entries:
            self._last_message_id = new_entries[-1][0]
        return new_entries

    async def _handle_stream_entry(self, message_id: str, data: Mapping[str, Any]) -> None:
        payload = _normalise_stream_payload(data)
        if not payload:
            return
        updated = await self._apply_order_event(payload)
        if updated:
            async with self._lock:
                self._last_updated_at = _utc_now()
                await self._persist_snapshot()
            await self._append_history({"event": "stream", "message_id": message_id, "payload": payload})

    async def _apply_order_event(self, payload: Mapping[str, Any]) -> bool:
        command_id = str(payload.get("command_id") or payload.get("ib_command_id") or "").strip()
        result = payload.get("result") if isinstance(payload.get("result"), Mapping) else None
        order_id = self._extract_order_id(payload, result, command_id)
        status = self._extract_status(payload, result)
        if not (command_id or order_id):
            descriptor = str(payload.get("type") or payload.get("category") or "").lower()
            if "order" not in descriptor:
                return False
        identifier = order_id or command_id
        if not identifier:
            return False
        async with self._lock:
            record = self._orders.get(identifier)
            if record is None and command_id and command_id in self._command_to_order:
                identifier = self._command_to_order[command_id]
                record = self._orders.get(identifier)
            if record is None:
                record = {
                    "id": identifier,
                    "command_id": command_id or identifier,
                }
            if command_id:
                record["command_id"] = command_id
                self._command_to_order[command_id] = identifier
            if order_id:
                record["order_id"] = order_id
            if isinstance(result, Mapping):
                record.setdefault("result", {})
                if isinstance(record["result"], Mapping):
                    merged = dict(record["result"])
                    merged.update(result)
                    record["result"] = merged
                else:
                    record["result"] = dict(result)
            record.setdefault("events", [])
            record["events"].append(dict(payload))
            if status:
                record["status"] = status
            self._orders[identifier] = record
            terminal = {"filled", "cancelled", "inactive", "completed", "rejected", "failed"}
            if status and status.lower() in terminal:
                self._orders.pop(identifier, None)
            elif status:
                record["status"] = status
            return True

    def _extract_order_id(
        self,
        payload: Mapping[str, Any],
        result: Mapping[str, Any] | None,
        command_id: str,
    ) -> str:
        for candidate in (
            payload.get("order_id"),
            payload.get("id"),
            payload.get("ib_order_id"),
            payload.get("permid"),
        ):
            if candidate:
                return str(candidate)
        if result:
            for candidate in (
                result.get("order_id"),
                result.get("id"),
                result.get("ib_order_id"),
                result.get("permid"),
            ):
                if candidate:
                    return str(candidate)
        return command_id

    def _extract_status(
        self,
        payload: Mapping[str, Any],
        result: Mapping[str, Any] | None,
    ) -> str:
        candidates: Iterable[Any] = ()
        if result:
            candidates = (
                result.get("order_status"),
                result.get("status"),
                result.get("orderStatus"),
            )
            for candidate in candidates:
                if candidate:
                    return str(candidate)
        for key in ("order_status", "orderStatus"):
            value = payload.get(key)
            if value:
                return str(value)
        status = payload.get("status")
        if status and str(status).lower() not in {"success", "error"}:
            return str(status)
        return ""


def create_app(runtime: OrdersRuntime | None = None, *, enable_events: bool = True) -> FastAPI:
    app = FastAPI(title="Orders Service", version="1.0.0")
    runtime_instance = runtime or OrdersRuntime()

    async def get_runtime() -> OrdersRuntime:
        return runtime_instance

    if enable_events:
        @app.on_event("startup")
        async def _startup() -> None:
            await runtime_instance.start()

        @app.on_event("shutdown")
        async def _shutdown() -> None:
            await runtime_instance.stop()

    @app.get("/orders", status_code=status.HTTP_200_OK)
    async def list_orders(runtime: OrdersRuntime = Depends(get_runtime)) -> Mapping[str, Any]:
        return await runtime.list_orders()

    @app.post("/orders", status_code=status.HTTP_202_ACCEPTED)
    async def place_order(
        request: OrderSubmission,
        runtime: OrdersRuntime = Depends(get_runtime),
    ) -> Mapping[str, Any]:
        command_id = await runtime.submit_order(request)
        return {"command_id": command_id, "status": "accepted"}

    @app.post("/orders/{order_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
    async def cancel_order(
        order_id: str,
        runtime: OrdersRuntime = Depends(get_runtime),
    ) -> Mapping[str, Any]:
        command_id = await runtime.cancel_order(order_id)
        return {"command_id": command_id, "status": "accepted"}

    @app.post("/orders/cancel_all", status_code=status.HTTP_202_ACCEPTED)
    async def cancel_all_orders(runtime: OrdersRuntime = Depends(get_runtime)) -> Mapping[str, Any]:
        command_id = await runtime.cancel_all()
        return {"command_id": command_id, "status": "accepted"}

    @app.post("/positions/{symbol}/close", status_code=status.HTTP_202_ACCEPTED)
    async def close_position(
        symbol: str,
        runtime: OrdersRuntime = Depends(get_runtime),
    ) -> Mapping[str, Any]:
        if not symbol:
            raise HTTPException(status_code=400, detail="symbol required")
        command_id = await runtime.close_position(symbol)
        return {"command_id": command_id, "status": "accepted"}

    @app.post("/positions/{symbol}/reverse", status_code=status.HTTP_202_ACCEPTED)
    async def reverse_position(
        symbol: str,
        runtime: OrdersRuntime = Depends(get_runtime),
    ) -> Mapping[str, Any]:
        if not symbol:
            raise HTTPException(status_code=400, detail="symbol required")
        command_id = await runtime.reverse_position(symbol)
        return {"command_id": command_id, "status": "accepted"}

    return app


app = create_app()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the orders service HTTP server")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Bind host (default: %(default)s)")
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="Service port (default: %(default)s)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable uvicorn reload (development only)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, reload=args.reload, log_level="info")


if __name__ == "__main__":  # pragma: no cover - manual execution path
    main()
