import asyncio
import copy
import inspect
import json
import logging
import os
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Deque, Dict, Iterable, Mapping

from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel, ConfigDict

from src.ibbot import update_service_health
from src.ibbot.influx import build_order_point, init_influx_client, write_points
from src.ibbot.redis import DEFAULT_COMMAND_QUEUE, enqueue_ib_command, get_redis_client

LOGGER = logging.getLogger("ibbot.services.orders")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _configured_timezone() -> str:
    return os.getenv("TIME_ZONE", "Asia/Shanghai")


def _serialise_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.isoformat()


def _maybe_json_loads(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except Exception:
            return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return value
        return parsed
    return value


async def _maybe_await(result: Awaitable[Any] | Any) -> Any:
    if inspect.isawaitable(result):
        return await result  # type: ignore[return-value]
    return result


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


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except Exception:
            return None
    return None


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _coerce_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        return str(value)
    return str(value)


def _first_nonempty(*candidates: Any) -> Any:
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            continue
        if candidate is None:
            continue
        if isinstance(candidate, str):
            text = candidate.strip()
            if not text:
                continue
            return text
        return candidate
    return None


_REQUEST_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "symbol": ("symbol", "ticker"),
    "action": ("action", "side"),
    "quantity": ("quantity", "total_quantity", "totalQuantity", "qty"),
    "order_type": ("order_type", "orderType", "type"),
    "limit_price": ("limit_price", "lmt_price", "limitPrice", "price"),
    "stop_price": ("stop_price", "aux_price", "stopPrice"),
    "source": ("source",),
    "strategy": ("strategy", "strategy_id"),
    "time_in_force": ("time_in_force", "tif", "tif_code"),
}


_RESULT_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "order_id": ("order_id", "ib_order_id", "permid", "permId", "id"),
    "status": ("order_status", "status", "orderStatus"),
    "filled_quantity": ("filled", "filled_quantity", "filledQuantity"),
    "remaining_quantity": (
        "remaining",
        "remaining_quantity",
        "remainingQuantity",
        "left",
    ),
    "avg_fill_price": ("avg_fill_price", "avg_price", "avgPrice"),
    "limit_price": ("limit_price", "lmt_price", "limitPrice"),
    "stop_price": ("stop_price", "aux_price", "stopPrice"),
    "commission": ("commission",),
    "realized_pnl": ("realized_pnl", "realizedPnl"),
    "unrealized_pnl": ("unrealized_pnl", "unrealizedPnl"),
    "submitted_at": ("submitted_at", "submitted", "submittedTime"),
    "completed_at": (
        "completed_at",
        "filled_at",
        "filledTime",
        "completion_time",
    ),
    "updated_at": ("timestamp", "updated_at", "time", "last_update"),
    "strategy": ("strategy", "strategy_id"),
    "source": ("source",),
}


def _coerce_mapping(value: Any) -> dict[str, Any] | None:
    value = _maybe_json_loads(value)
    if isinstance(value, Mapping):
        return dict(value)
    return None


def _apply_field_aliases(record: dict[str, Any], data: Mapping[str, Any], *, aliases: Mapping[str, Iterable[str]]) -> None:
    for canonical, options in aliases.items():
        for key in options:
            if key in data:
                candidate = data[key]
                if candidate is not None and candidate != "":
                    record[canonical] = candidate
                break


def _initial_order_record(command_id: str, request_data: Mapping[str, Any]) -> dict[str, Any]:
    now = _utc_now()
    timestamp = _serialise_datetime(now)
    record: dict[str, Any] = {
        "id": command_id,
        "command_id": command_id,
        "status": "pending",
        "requested_at": timestamp,
        "updated_at": timestamp,
        "request": dict(request_data),
        "events": [],
    }
    _apply_field_aliases(record, request_data, aliases=_REQUEST_FIELD_ALIASES)
    record.setdefault("symbol", request_data.get("symbol"))
    record.setdefault("action", request_data.get("action"))
    record.setdefault("quantity", request_data.get("quantity"))
    submission_event = {
        "type": "submission",
        "timestamp": timestamp,
        "payload": dict(request_data),
    }
    record["events"].append(submission_event)
    return record


def _clone_order_record(record: Mapping[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(dict(record))


def _normalise_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    parsed = _parse_datetime(value)
    if parsed is not None:
        return _serialise_datetime(parsed)
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, datetime):
        return _serialise_datetime(value)
    return str(value)


def _normalise_order_record(record: Mapping[str, Any]) -> dict[str, Any]:
    payload = _clone_order_record(record)
    for key in (
        "requested_at",
        "updated_at",
        "submitted_at",
        "completed_at",
        "timestamp",
    ):
        if key in payload:
            payload[key] = _normalise_timestamp(payload.get(key))
    events = payload.get("events")
    if isinstance(events, list):
        normalised_events: list[dict[str, Any]] = []
        for item in events:
            if not isinstance(item, Mapping):
                continue
            event_payload = dict(item)
            if "timestamp" in event_payload:
                event_payload["timestamp"] = _normalise_timestamp(
                    event_payload.get("timestamp")
                )
            if isinstance(event_payload.get("payload"), Mapping):
                event_payload["payload"] = dict(event_payload["payload"])
            normalised_events.append(event_payload)
        payload["events"] = normalised_events
    return payload


def _normalise_cached_record(record: Mapping[str, Any]) -> dict[str, Any]:
    payload = _clone_order_record(record)

    request_mapping = _coerce_mapping(payload.get("request"))
    if request_mapping is None:
        request_mapping = {}
    payload["request"] = request_mapping

    result_mapping = _coerce_mapping(payload.get("result"))
    if result_mapping is not None:
        payload["result"] = result_mapping

    response_mapping = _coerce_mapping(payload.get("response"))
    if response_mapping is not None:
        payload["response"] = response_mapping

    sources: list[Mapping[str, Any]] = [payload, request_mapping]
    if result_mapping is not None:
        sources.append(result_mapping)
    if response_mapping is not None:
        sources.append(response_mapping)

    for source in sources:
        _apply_field_aliases(payload, source, aliases=_REQUEST_FIELD_ALIASES)
        _apply_field_aliases(payload, source, aliases=_RESULT_FIELD_ALIASES)

    for key in ("symbol", "action", "source", "strategy", "order_type"):
        if key in payload:
            coerced = _coerce_str(payload.get(key))
            if coerced is not None:
                payload[key] = coerced

    for key in ("limit_price", "avg_fill_price", "stop_price"):
        if key in payload:
            coerced_float = _coerce_float(payload.get(key))
            if coerced_float is not None:
                payload[key] = coerced_float

    for key in ("command_id", "order_id", "id"):
        if key in payload:
            coerced_id = _coerce_str(payload.get(key))
            if coerced_id is not None:
                payload[key] = coerced_id

    events = payload.get("events")
    if isinstance(events, Iterable) and not isinstance(events, (str, bytes, bytearray)):
        normalised_events: list[dict[str, Any]] = []
        for event in events:
            if isinstance(event, Mapping):
                normalised_events.append(dict(event))
        payload["events"] = normalised_events
    else:
        payload["events"] = []

    return payload


def _event_timestamp(payload: Mapping[str, Any]) -> str:
    for key in ("timestamp", "time", "created_at", "event_time", "updated_at"):
        value = payload.get(key)
        if value:
            if isinstance(value, datetime):
                return _serialise_datetime(value)
            if isinstance(value, str):
                return value
    return _serialise_datetime(_utc_now())


_TERMINAL_STATUSES = {
    "filled",
    "completed",
    "cancelled",
    "inactive",
    "rejected",
    "failed",
    "expired",
}


def _record_matches(
    record: Mapping[str, Any],
    identifier: str,
    order_id: str | None,
    command_id: str | None,
) -> bool:
    candidates = {
        str(record.get("id") or ""),
        str(record.get("order_id") or ""),
        str(record.get("command_id") or ""),
    }
    candidates = {value for value in candidates if value}
    if identifier and identifier in candidates:
        return True
    if order_id and order_id in candidates:
        return True
    if command_id and command_id in candidates:
        return True
    return False


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
    write_fn: Callable[[Iterable[Any]], Awaitable[int] | int] = write_points
    command_queue: str = DEFAULT_COMMAND_QUEUE
    results_stream: str = "stream:ib:results"
    snapshot_key: str = "json:orders:snapshot"
    history_key: str = "list:orders:history"
    stream_poll_interval: float = 1.0
    health_interval: float = 15.0
    history_limit: int = 200
    logger: logging.Logger = field(default_factory=lambda: LOGGER)
    default_timezone: str = field(default_factory=_configured_timezone)

    _redis: Any = field(default=None, init=False, repr=False)
    _active_orders: Dict[str, Dict[str, Any]] = field(
        default_factory=dict, init=False, repr=False
    )
    _history: Deque[Dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=200), init=False, repr=False
    )
    _command_to_order: Dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _last_message_id: str | None = field(default=None, init=False, repr=False)
    _last_updated_at: datetime | None = field(default=None, init=False, repr=False)
    _stream_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _health_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _influx_initialised: bool = field(default=False, init=False, repr=False)
    _influx_disabled: bool = field(default=False, init=False, repr=False)
    _influx_disable_reason: str = field(default="", init=False, repr=False)

    def __post_init__(self) -> None:
        self._history = deque(maxlen=self.history_limit)

    def _configure_influx(self) -> None:
        if self._influx_initialised:
            return
        self._influx_initialised = True
        try:
            init_influx_client()
        except Exception as exc:  # pragma: no cover - depends on optional dependency
            self._influx_disabled = True
            self._influx_disable_reason = str(exc)
            self.logger.info("Orders Influx integration disabled: %s", exc)
        else:
            self._influx_disabled = False
            self._influx_disable_reason = ""

    async def start(self) -> None:
        if self._stream_task is not None or self._redis is not None:
            return

        self._configure_influx()
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

        request_payload = request.model_dump(mode="json")
        entry = _initial_order_record(command_id, request_payload)
        entry_snapshot: dict[str, Any] | None = None
        async with self._lock:
            self._active_orders[command_id] = entry
            self._command_to_order[command_id] = command_id
            self._last_updated_at = _utc_now()
            await self._persist_snapshot()
            await self._append_history({"event": "submitted", "command_id": command_id, "order": entry})
            entry_snapshot = _clone_order_record(entry)
        if entry_snapshot is not None:
            await self._write_order_point(entry_snapshot, timestamp=entry_snapshot.get("requested_at"))
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

    async def list_orders(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        start_date: datetime | str | None = None,
        tz: str | None = None,
    ) -> dict[str, Any]:
        if limit < 0:
            limit = 0
        if offset < 0:
            offset = 0

        start_dt: datetime | None
        if isinstance(start_date, datetime):
            start_dt = start_date
        elif isinstance(start_date, str):
            start_dt = _parse_datetime(start_date)
        else:
            start_dt = None

        effective_timezone = tz if tz else None
        tzinfo: ZoneInfo | None = None
        if start_dt is None:
            timezone_candidates = [tz, self.default_timezone, "UTC"]
            for candidate in timezone_candidates:
                if not candidate:
                    continue
                try:
                    tzinfo = ZoneInfo(candidate)
                except Exception:
                    continue
                else:
                    effective_timezone = candidate
                    break
            if tzinfo is not None:
                now = _utc_now().astimezone(tzinfo)
            else:
                now = _utc_now()
            start_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            if tz:
                try:
                    tzinfo = ZoneInfo(tz)
                except Exception:
                    tzinfo = None
                else:
                    effective_timezone = tz
            if tzinfo is not None:
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=tzinfo)
                elif tz:
                    start_dt = start_dt.astimezone(tzinfo)
            elif start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)

        async with self._lock:
            combined: list[dict[str, Any]] = []
            combined.extend(self._active_orders.values())
            combined.extend(self._history)
            ordered = sorted(
                (_normalise_order_record(order) for order in combined),
                key=lambda item: item.get("updated_at") or item.get("requested_at") or "",
                reverse=True,
            )

            if start_dt is not None:
                threshold = start_dt
                filtered_orders: list[dict[str, Any]] = []

                def _record_timestamp(record: Mapping[str, Any]) -> datetime | None:
                    timestamp: datetime | None = None
                    for key in (
                        "updated_at",
                        "submitted_at",
                        "requested_at",
                        "completed_at",
                        "created_at",
                        "timestamp",
                    ):
                        candidate = record.get(key)
                        parsed = _parse_datetime(candidate)
                        if parsed is None and isinstance(candidate, str):
                            try:
                                parsed = datetime.fromisoformat(candidate)
                            except Exception:
                                continue
                        if parsed is None:
                            continue
                        if parsed.tzinfo is None:
                            if threshold.tzinfo is not None:
                                parsed = parsed.replace(tzinfo=threshold.tzinfo)
                            else:
                                parsed = parsed.replace(tzinfo=timezone.utc)
                        elif threshold.tzinfo is not None:
                            parsed = parsed.astimezone(threshold.tzinfo)
                        timestamp = parsed
                        break
                    return timestamp

                for order in ordered:
                    order_timestamp = _record_timestamp(order)
                    if order_timestamp is None or order_timestamp >= threshold:
                        filtered_orders.append(order)
                ordered = filtered_orders

            total = len(ordered)
            if limit:
                sliced = ordered[offset : offset + limit]
            else:
                sliced = ordered[offset:]
            updated_at = self._last_updated_at
            last_event_id = self._last_message_id or ""
            page = offset // limit if limit else 0
        start_iso = _serialise_datetime(start_dt) if start_dt is not None else None
        return {
            "orders": sliced,
            "total": total,
            "limit": limit,
            "offset": offset,
            "page": page,
            "updated_at": _serialise_datetime(updated_at),
            "last_event_id": last_event_id,
            "start_date": start_iso,
            "start_date_timezone": effective_timezone if effective_timezone else None,
        }

    async def get_order(self, order_id: str) -> dict[str, Any]:
        identifier = str(order_id or "").strip()
        if not identifier:
            raise KeyError(order_id)
        async with self._lock:
            record = self._find_order_unlocked(identifier)
            updated_at = _normalise_timestamp(self._last_updated_at)
            last_event_id = self._last_message_id or ""
        if record is None and self._redis is not None:
            await self._load_snapshot()
            async with self._lock:
                record = self._find_order_unlocked(identifier)
                updated_at = _normalise_timestamp(self._last_updated_at)
                last_event_id = self._last_message_id or last_event_id
        if record is None:
            raise KeyError(order_id)
        order_payload = _normalise_order_record(record)
        return {
            "success": True,
            "order": order_payload,
            "updated_at": updated_at,
            "last_event_id": last_event_id,
        }

    async def get_order_events(self, order_id: str) -> dict[str, Any]:
        payload = await self.get_order(order_id)
        order_payload = payload.get("order")
        events: list[Mapping[str, Any]]
        if isinstance(order_payload, Mapping) and isinstance(order_payload.get("events"), list):
            events = [
                event
                for event in order_payload["events"]
                if isinstance(event, Mapping)
            ]
        else:
            events = []
        normalised_events = [dict(event) for event in events]
        for event in normalised_events:
            if "timestamp" in event:
                event["timestamp"] = _normalise_timestamp(event.get("timestamp"))
            if isinstance(event.get("payload"), Mapping):
                event["payload"] = dict(event["payload"])
        order_identifier = None
        if isinstance(order_payload, Mapping):
            for key in ("order_id", "id", "command_id"):
                value = order_payload.get(key)
                if value:
                    order_identifier = str(value)
                    break
        return {
            "success": True,
            "order_id": order_identifier or str(order_id),
            "events": normalised_events,
            "updated_at": payload.get("updated_at"),
            "last_event_id": payload.get("last_event_id"),
        }

    def _find_order_unlocked(self, order_id: str) -> dict[str, Any] | None:
        identifier = str(order_id).strip()
        if not identifier:
            return None
        direct = self._command_to_order.get(identifier)
        if direct:
            if direct in self._active_orders:
                return self._active_orders[direct]
            for entry in self._history:
                if _record_matches(entry, direct, direct, direct):
                    return entry
        if identifier in self._active_orders:
            return self._active_orders[identifier]
        for record in self._active_orders.values():
            if _record_matches(record, identifier, identifier, identifier):
                return record
        for record in self._history:
            if _record_matches(record, identifier, identifier, identifier):
                return record
        return None

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
        active_payload = payload.get("active_orders")
        history_payload = payload.get("history")
        if active_payload is None and payload.get("orders") is not None:
            active_payload = payload.get("orders")
        active_items: Iterable[Any]
        if isinstance(active_payload, Mapping):
            active_items = active_payload.values()
        elif isinstance(active_payload, Iterable):
            active_items = active_payload
        else:
            active_items = []
        history_items: Iterable[Any]
        if isinstance(history_payload, Iterable):
            history_items = history_payload
        else:
            history_items = []
        async with self._lock:
            self._active_orders = {}
            self._history = deque(maxlen=self.history_limit)
            self._command_to_order = {}
            for item in active_items:
                if not isinstance(item, Mapping):
                    continue
                record = _normalise_cached_record(item)
                identifier = str(
                    record.get("id")
                    or record.get("order_id")
                    or record.get("command_id")
                    or record.get("ib_order_id")
                    or ""
                )
                if not identifier:
                    continue
                record.setdefault("command_id", identifier)
                self._active_orders[identifier] = record
                if record.get("command_id"):
                    self._command_to_order[str(record["command_id"])] = identifier
                if record.get("order_id"):
                    self._command_to_order[str(record["order_id"])] = identifier
            for item in history_items:
                if not isinstance(item, Mapping):
                    continue
                record = _normalise_cached_record(item)
                identifier = str(
                    record.get("id")
                    or record.get("order_id")
                    or record.get("command_id")
                    or ""
                )
                if identifier:
                    self._command_to_order[str(record.get("command_id", identifier))] = identifier
                if record.get("order_id"):
                    self._command_to_order[str(record["order_id"])] = identifier
                self._history.appendleft(record)
            timestamp = payload.get("updated_at")
            if isinstance(timestamp, str) and timestamp:
                try:
                    self._last_updated_at = datetime.fromisoformat(timestamp)
                except Exception:
                    self._last_updated_at = None
            last_event_id = payload.get("last_event_id")
            if isinstance(last_event_id, str) and last_event_id:
                self._last_message_id = last_event_id

    async def _persist_snapshot(self) -> None:
        if self._redis is None:
            return
        active_orders = [_clone_order_record(order) for order in self._active_orders.values()]
        history_orders = [_clone_order_record(order) for order in list(self._history)]
        snapshot = {
            "active_orders": active_orders,
            "history": history_orders,
            "updated_at": _serialise_datetime(self._last_updated_at),
            "last_event_id": self._last_message_id or "",
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

    def _build_influx_order_point(
        self,
        record: Mapping[str, Any],
        *,
        timestamp: str | datetime | None = None,
    ) -> Any | None:
        request = _coerce_mapping(record.get("request")) or {}
        response = _coerce_mapping(record.get("response")) or {}
        result = _coerce_mapping(record.get("result")) or {}
        sources: tuple[Mapping[str, Any], ...] = tuple(
            source for source in (record, result, response, request) if isinstance(source, Mapping)
        )

        def _lookup(*keys: str) -> Any:
            for source in sources:
                for key in keys:
                    if key in source:
                        value = source[key]
                        if value is None:
                            continue
                        if isinstance(value, str) and not value.strip():
                            continue
                        return value
            return None

        order_identifier = _first_nonempty(
            _lookup("order_id", "ib_order_id", "permid", "permId"),
            record.get("id"),
            record.get("command_id"),
        )
        symbol_value = _first_nonempty(_lookup("symbol", "ticker"))
        if not order_identifier or not symbol_value:
            return None

        side_value = _first_nonempty(_lookup("action", "side"))
        side = str(side_value).upper() if side_value is not None else "UNKNOWN"

        exchange_value = _first_nonempty(_lookup("exchange"))
        exchange = _coerce_str(exchange_value)

        quantity_value = _coerce_float(
            _lookup(
                "quantity",
                "total_quantity",
                "totalQuantity",
                "qty",
                "filled_quantity",
                "filledQuantity",
            )
        )
        quantity = quantity_value if quantity_value is not None else 0.0

        price_value = _coerce_float(
            _lookup(
                "avg_fill_price",
                "avg_price",
                "avgPrice",
                "limit_price",
                "lmt_price",
                "limitPrice",
                "stop_price",
                "aux_price",
                "stopPrice",
                "price",
            )
        )
        price = price_value if price_value is not None else 0.0

        status_value = _first_nonempty(
            record.get("status"),
            _lookup("status", "order_status", "orderStatus"),
        )
        status = str(status_value) if status_value is not None else "unknown"

        timestamp_dt = _parse_datetime(timestamp)
        if timestamp_dt is None:
            timestamp_dt = _parse_datetime(record.get("updated_at"))
        if timestamp_dt is None:
            timestamp_dt = _parse_datetime(record.get("requested_at"))
        if timestamp_dt is None:
            timestamp_dt = _utc_now()

        extra_fields: dict[str, Any] = {}

        float_fields: dict[str, tuple[str, ...]] = {
            "filled_quantity": (
                "filled_quantity",
                "filled",
                "filledQuantity",
            ),
            "remaining_quantity": (
                "remaining_quantity",
                "remaining",
                "remainingQuantity",
                "left",
            ),
            "avg_fill_price": ("avg_fill_price", "avg_price", "avgPrice"),
            "limit_price": ("limit_price", "lmt_price", "limitPrice"),
            "stop_price": ("stop_price", "aux_price", "stopPrice"),
            "commission": ("commission",),
            "realized_pnl": ("realized_pnl", "realizedPnl"),
            "unrealized_pnl": ("unrealized_pnl", "unrealizedPnl"),
            "trigger_price": ("trigger_price",),
        }
        for field_name, keys in float_fields.items():
            value = None
            for key in keys:
                candidate = _lookup(key)
                if candidate is not None:
                    value = candidate
                    break
            coerced = _coerce_float(value)
            if coerced is not None:
                extra_fields[field_name] = coerced

        string_fields: dict[str, tuple[str, ...]] = {
            "order_type": ("order_type", "orderType", "type"),
            "trigger": ("trigger",),
            "trigger_type": ("trigger_type",),
            "trigger_status": ("trigger_status", "triggerStatus"),
        }
        for field_name, keys in string_fields.items():
            value = None
            for key in keys:
                candidate = _lookup(key)
                if candidate is not None:
                    value = candidate
                    break
            text = _coerce_str(value)
            if text:
                extra_fields[field_name] = text

        extra_tags: dict[str, str] = {}
        tag_fields: dict[str, tuple[str, ...]] = {
            "account": ("account",),
            "source": ("source",),
            "strategy": ("strategy", "strategy_id"),
            "sec_type": ("sec_type", "security_type", "secType"),
        }
        for tag_name, keys in tag_fields.items():
            value = None
            for key in keys:
                candidate = _lookup(key)
                if candidate is not None:
                    value = candidate
                    break
            text = _coerce_str(value)
            if text:
                extra_tags[tag_name] = text

        return build_order_point(
            order_id=str(order_identifier),
            symbol=str(symbol_value),
            side=side,
            quantity=quantity,
            price=price,
            status=status,
            exchange=exchange,
            timestamp=timestamp_dt,
            extra_fields=extra_fields or None,
            extra_tags=extra_tags or None,
        )

    async def _write_order_point(
        self,
        record: Mapping[str, Any],
        *,
        timestamp: str | datetime | None = None,
    ) -> None:
        if self._influx_disabled:
            return
        if not self._influx_initialised:
            self._configure_influx()
            if self._influx_disabled:
                return

        point = self._build_influx_order_point(record, timestamp=timestamp)
        if point is None:
            return
        try:
            await _maybe_await(self.write_fn([point]))
        except ValueError as exc:
            message = str(exc)
            if not self._influx_disabled:
                self.logger.info("Disabling orders Influx writes: %s", message)
            self._influx_disabled = True
            self._influx_disable_reason = message
        except Exception:
            self.logger.warning("Failed to write order point to InfluxDB", exc_info=True)

    async def _publish_health(self) -> None:
        if self._redis is None:
            return
        async with self._lock:
            metrics = {
                "active_orders": len(self._active_orders),
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
        command_id = str(
            payload.get("command_id") or payload.get("ib_command_id") or ""
        ).strip()
        parsed_payload = dict(payload)
        result_mapping = _coerce_mapping(parsed_payload.get("result"))
        if result_mapping is not None:
            parsed_payload["result"] = result_mapping
        response_mapping = _coerce_mapping(parsed_payload.get("response"))
        if response_mapping is not None:
            parsed_payload["response"] = response_mapping
        order_id = self._extract_order_id(parsed_payload, result_mapping, command_id)
        status = self._extract_status(parsed_payload, result_mapping)
        if not (command_id or order_id):
            descriptor = str(parsed_payload.get("type") or parsed_payload.get("category") or "").lower()
            if "order" not in descriptor:
                return False
        identifier = order_id or command_id
        if not identifier:
            return False
        event_time = _event_timestamp(parsed_payload)
        updated = False
        record_snapshot: dict[str, Any] | None = None
        async with self._lock:
            record_identifier = identifier
            record = self._active_orders.get(record_identifier)
            from_history = False
            if record is None and command_id and command_id in self._command_to_order:
                record_identifier = self._command_to_order[command_id]
                record = self._active_orders.get(record_identifier)
            if record is None and order_id and order_id in self._command_to_order:
                record_identifier = self._command_to_order[order_id]
                record = self._active_orders.get(record_identifier)
            if record is None:
                for item in self._history:
                    if _record_matches(item, identifier, order_id, command_id):
                        record = item
                        record_identifier = str(item.get("id") or identifier)
                        from_history = True
                        break
            if record is None:
                record_identifier = identifier
                record = {
                    "id": identifier,
                    "command_id": command_id or identifier,
                    "events": [],
                }
            if "events" not in record or not isinstance(record["events"], list):
                record["events"] = list(record.get("events", []))
            record.setdefault("id", record_identifier)
            self._command_to_order.setdefault(record_identifier, record_identifier)
            if command_id:
                record["command_id"] = command_id
                self._command_to_order[command_id] = record_identifier
            if order_id:
                record["order_id"] = order_id
                self._command_to_order[order_id] = record_identifier
            record.setdefault("request", {})
            request_mapping = _coerce_mapping(parsed_payload.get("request"))
            if request_mapping is not None:
                record["request"] = request_mapping
                _apply_field_aliases(record, request_mapping, aliases=_REQUEST_FIELD_ALIASES)
            if result_mapping is not None:
                record.setdefault("result", {})
                existing_result = (
                    dict(record["result"])
                    if isinstance(record.get("result"), Mapping)
                    else {}
                )
                existing_result.update(result_mapping)
                record["result"] = existing_result
            if response_mapping is not None:
                record["response"] = response_mapping
                _apply_field_aliases(record, response_mapping, aliases=_RESULT_FIELD_ALIASES)
            _apply_field_aliases(record, parsed_payload, aliases=_REQUEST_FIELD_ALIASES)
            if result_mapping is not None:
                _apply_field_aliases(record, result_mapping, aliases=_REQUEST_FIELD_ALIASES)
                _apply_field_aliases(record, result_mapping, aliases=_RESULT_FIELD_ALIASES)
            _apply_field_aliases(record, parsed_payload, aliases=_RESULT_FIELD_ALIASES)
            status_lower = status.lower() if isinstance(status, str) else ""
            if status:
                record["status"] = status
            record["updated_at"] = event_time
            if status_lower in {"filled", "cancelled"}:
                record.setdefault("completed_at", event_time)
            elif status_lower in _TERMINAL_STATUSES:
                record.setdefault("completed_at", event_time)
            record.setdefault("events", [])
            record["events"].append(
                {
                    "type": parsed_payload.get("type") or parsed_payload.get("category") or "event",
                    "timestamp": event_time,
                    "payload": parsed_payload,
                }
            )
            if from_history and status_lower not in _TERMINAL_STATUSES:
                retained = [
                    _clone_order_record(item)
                    for item in self._history
                    if not _record_matches(item, record_identifier, order_id, command_id)
                ]
                self._history = deque(retained, maxlen=self.history_limit)
                from_history = False
                self._active_orders[record_identifier] = record
            if not from_history:
                self._active_orders[record_identifier] = record
            if status_lower in _TERMINAL_STATUSES:
                self._active_orders.pop(record_identifier, None)
                # remove duplicates from history then add latest snapshot
                existing_history = [
                    _clone_order_record(item)
                    for item in self._history
                    if not _record_matches(item, record_identifier, order_id, command_id)
                ]
                existing_history.insert(0, _clone_order_record(record))
                self._history = deque(existing_history, maxlen=self.history_limit)
            record_snapshot = _clone_order_record(record)
            updated = True
        if record_snapshot is not None:
            await self._write_order_point(record_snapshot, timestamp=event_time)
        return updated

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
    async def list_orders(
        limit: int = 50,
        offset: int = 0,
        start_date: str | None = None,
        start_date_tz: str | None = None,
        runtime: OrdersRuntime = Depends(get_runtime),
    ) -> Mapping[str, Any]:
        return await runtime.list_orders(
            limit=limit,
            offset=offset,
            start_date=start_date,
            tz=start_date_tz,
        )

    @app.get("/orders/{order_id}", status_code=status.HTTP_200_OK)
    async def get_order(
        order_id: str,
        runtime: OrdersRuntime = Depends(get_runtime),
    ) -> Mapping[str, Any]:
        try:
            return await runtime.get_order(order_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="order not found")

    @app.get("/orders/{order_id}/events", status_code=status.HTTP_200_OK)
    async def get_order_events(
        order_id: str,
        runtime: OrdersRuntime = Depends(get_runtime),
    ) -> Mapping[str, Any]:
        try:
            return await runtime.get_order_events(order_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="order not found")

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
