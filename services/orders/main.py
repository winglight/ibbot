import asyncio
import copy
import inspect
import json
import logging
import os
import uuid
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo as datetime_tzinfo
from typing import Any, Awaitable, Callable, Deque, Dict, Iterable, Mapping, Protocol, runtime_checkable

from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel, ConfigDict, model_validator

from src.ibbot import update_service_health
from src.ibbot.redis import DEFAULT_COMMAND_QUEUE, enqueue_ib_command, get_redis_client


@runtime_checkable
class OrderRepositoryProtocol(Protocol):
    async def upsert_order(self, payload: Mapping[str, Any]) -> int | None:
        ...

LOGGER = logging.getLogger("ibbot.services.orders")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _configured_timezone() -> str:
    return os.getenv("TIME_ZONE", "Asia/Shanghai")


def _serialise_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.isoformat()


def _extract_timezone_name(
    tzinfo: datetime_tzinfo | None, reference: datetime | None = None
) -> str | None:
    if tzinfo is None:
        return None
    if hasattr(tzinfo, "key"):
        key = getattr(tzinfo, "key")
        if isinstance(key, str):
            return key
    if reference is None:
        reference = _utc_now()
    name = tzinfo.tzname(reference)
    if isinstance(name, str) and name:
        return name
    return None


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


def _resolve_start_date(
    start_date: datetime | str | None,
    tz: str | None,
    *,
    default_timezone: str,
) -> tuple[datetime, str | None]:
    start_dt: datetime | None
    if isinstance(start_date, datetime):
        start_dt = start_date
    elif isinstance(start_date, str):
        start_dt = _parse_datetime(start_date)
    else:
        start_dt = None

    tzinfo: datetime_tzinfo | None = None
    effective_timezone: str | None = tz if tz else None

    if start_dt is None:
        timezone_candidates = [tz, default_timezone, "UTC"]
        for candidate in timezone_candidates:
            if not candidate:
                continue
            try:
                tzinfo = ZoneInfo(candidate)
            except Exception:
                continue
            effective_timezone = candidate
            break
        if tzinfo is None:
            tzinfo = timezone.utc
            effective_timezone = "UTC"
        now = _utc_now().astimezone(tzinfo)
        start_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        if tz:
            try:
                tzinfo = ZoneInfo(tz)
            except Exception:
                tzinfo = None
            else:
                effective_timezone = tz
        if start_dt.tzinfo is None:
            if tzinfo is None:
                try:
                    tzinfo = ZoneInfo(default_timezone)
                    effective_timezone = default_timezone
                except Exception:
                    tzinfo = timezone.utc
                    effective_timezone = "UTC"
            start_dt = start_dt.replace(tzinfo=tzinfo)
        elif tzinfo is not None:
            start_dt = start_dt.astimezone(tzinfo)
        else:
            tzinfo = start_dt.tzinfo
            if effective_timezone is None:
                effective_timezone = _extract_timezone_name(tzinfo, start_dt)

    if effective_timezone is None:
        effective_timezone = _extract_timezone_name(start_dt.tzinfo, start_dt)

    return start_dt, effective_timezone


_REQUEST_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "symbol": ("symbol", "ticker"),
    "action": ("action", "side"),
    "quantity": ("quantity", "total_quantity", "totalQuantity", "qty"),
    "order_type": ("order_type", "orderType", "type"),
    "limit_price": ("limit_price", "lmt_price", "limitPrice", "price"),
    "stop_price": ("stop_price", "aux_price", "stopPrice"),
    "source": ("source", "metadata.source"),
    "strategy": ("strategy", "strategy_id", "metadata.strategy"),
    "time_in_force": ("time_in_force", "tif", "tif_code", "metadata.time_in_force"),
    "notes": ("notes", "note", "comment", "metadata.notes"),
}


_RESULT_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "symbol": (
        "symbol",
        "contract.symbol",
        "contract.localSymbol",
        "contract.ticker",
        "instrument.ticker",
    ),
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


def _resolve_alias_value(data: Mapping[str, Any], key: str) -> Any:
    if not isinstance(data, Mapping):
        return None
    if "." not in key:
        return data.get(key)
    current: Any = data
    for part in key.split("."):
        if not isinstance(current, Mapping):
            return None
        if part not in current:
            return None
        current = current[part]
        if current is None:
            return None
    return current


def _apply_field_aliases(record: dict[str, Any], data: Mapping[str, Any], *, aliases: Mapping[str, Iterable[str]]) -> None:
    for canonical, options in aliases.items():
        for key in options:
            candidate = _resolve_alias_value(data, key)
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
    record.setdefault("notes", request_data.get("notes"))
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
    metadata_mapping = _coerce_mapping(payload.get("metadata"))
    if metadata_mapping is not None:
        payload["metadata"] = metadata_mapping
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

    metadata_mapping = _coerce_mapping(payload.get("metadata"))
    if metadata_mapping is not None:
        payload["metadata"] = metadata_mapping

    sources: list[Mapping[str, Any]] = [payload, request_mapping]
    if result_mapping is not None:
        sources.append(result_mapping)
    if response_mapping is not None:
        sources.append(response_mapping)
    if metadata_mapping is not None:
        sources.append(metadata_mapping)

    for source in sources:
        _apply_field_aliases(payload, source, aliases=_REQUEST_FIELD_ALIASES)
        _apply_field_aliases(payload, source, aliases=_RESULT_FIELD_ALIASES)

    for key in ("symbol", "action", "source", "strategy", "order_type", "notes"):
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


class FillMetricsUpdate(BaseModel):
    filled_quantity: float | None = None
    remaining_quantity: float | None = None
    avg_fill_price: float | None = None
    commission: float | None = None
    realized_pnl: float | None = None
    unrealized_pnl: float | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _ensure_any_field(self) -> "FillMetricsUpdate":
        if not any(
            value is not None
            for value in (
                self.filled_quantity,
                self.remaining_quantity,
                self.avg_fill_price,
                self.commission,
                self.realized_pnl,
                self.unrealized_pnl,
            )
        ):
            raise ValueError("at least one fill metric must be provided")
        return self


@dataclass
class OrdersRuntime:
    redis_factory: Callable[[], Awaitable[Any]] = get_redis_client
    order_repository_factory: (
        Callable[[], Awaitable[OrderRepositoryProtocol] | OrderRepositoryProtocol]
        | None
    ) = None
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
    _order_repository: OrderRepositoryProtocol | None = field(
        default=None, init=False, repr=False
    )
    _order_repository_close: Callable[[], Awaitable[None] | None] | None = field(
        default=None, init=False, repr=False
    )
    _repository_failure_count: int = field(default=0, init=False, repr=False)
    _repository_last_error: str = field(default="", init=False, repr=False)

    def __post_init__(self) -> None:
        self._history = deque(maxlen=self.history_limit)

    async def start(self) -> None:
        if self._stream_task is not None or self._redis is not None:
            return

        try:
            if (
                self.order_repository_factory is not None
                and self._order_repository is None
            ):
                await self._initialise_order_repository()
            self._redis = await self.redis_factory()
            await self._load_snapshot()
            await self._publish_health()
        except Exception:
            await self._shutdown_order_repository()
            self._redis = None
            raise

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
        await self._shutdown_order_repository()

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
            inserted_id = await self._upsert_order_snapshot(
                entry_snapshot,
                timestamp=entry_snapshot.get("requested_at"),
            )
            if inserted_id is not None:
                await self._register_persisted_order_id(
                    command_id, entry_snapshot, inserted_id
                )
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

    async def update_fill_metrics(
        self, order_id: str, metrics: Mapping[str, Any]
    ) -> dict[str, Any]:
        identifier = str(order_id or "").strip()
        if not identifier:
            raise KeyError(order_id)

        numeric_fields = (
            "filled_quantity",
            "remaining_quantity",
            "avg_fill_price",
            "commission",
            "realized_pnl",
            "unrealized_pnl",
        )
        updates: dict[str, float] = {}
        for field in numeric_fields:
            if field not in metrics:
                continue
            value = _coerce_float(metrics.get(field))
            if value is None:
                continue
            updates[field] = value

        if not updates:
            raise ValueError("no fill metrics provided")

        timestamp_dt = _utc_now()
        timestamp = _serialise_datetime(timestamp_dt)
        record_snapshot: dict[str, Any] | None = None
        record_identifier_for_repo: str | None = None

        async with self._lock:
            record = self._find_order_unlocked(identifier)
            if record is None:
                raise KeyError(order_id)

            if not isinstance(record.get("events"), list):
                record["events"] = list(record.get("events", []))

            in_active = any(entry is record for entry in self._active_orders.values())
            in_history = any(entry is record for entry in self._history)

            result_mapping: dict[str, Any]
            existing_result = record.get("result")
            if isinstance(existing_result, Mapping):
                result_mapping = dict(existing_result)
            else:
                result_mapping = {}
            result_mapping.update(updates)
            record["result"] = result_mapping

            for key, value in updates.items():
                record[key] = value

            record.setdefault("id", identifier)
            record["updated_at"] = timestamp
            record.setdefault("events", [])
            record["events"].append(
                {
                    "type": "fill_metrics_updated",
                    "timestamp": timestamp,
                    "payload": dict(updates),
                }
            )

            record_snapshot = _clone_order_record(record)
            order_identifier = str(
                record_snapshot.get("order_id")
                or record_snapshot.get("command_id")
                or record_snapshot.get("id")
                or identifier
            )

            canonical_identifier = identifier
            if in_active:
                for key, entry in self._active_orders.items():
                    if entry is record:
                        canonical_identifier = str(key)
                        break
            elif record_snapshot.get("id"):
                canonical_identifier = str(record_snapshot.get("id"))
            else:
                canonical_identifier = order_identifier

            self._command_to_order[identifier] = canonical_identifier
            if record_snapshot.get("order_id"):
                self._command_to_order[str(record_snapshot["order_id"])] = canonical_identifier
            if record_snapshot.get("command_id"):
                self._command_to_order[str(record_snapshot["command_id"])] = canonical_identifier

            record_identifier_for_repo = canonical_identifier

            if not in_active and not in_history:
                self._history.appendleft(record_snapshot)
            elif in_history:
                retained = [
                    _clone_order_record(item)
                    for item in self._history
                    if not _record_matches(
                        item,
                        canonical_identifier,
                        str(record_snapshot.get("order_id") or ""),
                        str(record_snapshot.get("command_id") or ""),
                    )
                ]
                retained.insert(0, record_snapshot)
                self._history = deque(retained, maxlen=self.history_limit)

            self._last_updated_at = timestamp_dt
            await self._persist_snapshot()
            await self._append_history(
                {
                    "event": "fill_metrics_updated",
                    "order_id": order_identifier,
                    "payload": dict(updates),
                }
            )

        if record_snapshot is not None:
            inserted_id = await self._upsert_order_snapshot(
                record_snapshot, timestamp=timestamp
            )
            if inserted_id is not None and record_identifier_for_repo is not None:
                await self._register_persisted_order_id(
                    record_identifier_for_repo, record_snapshot, inserted_id
                )
            return record_snapshot
        raise RuntimeError("failed to update fill metrics")

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

        start_dt, effective_timezone = _resolve_start_date(
            start_date,
            tz,
            default_timezone=self.default_timezone,
        )

        threshold_tzinfo = start_dt.tzinfo

        async with self._lock:
            combined: list[dict[str, Any]] = []
            combined.extend(self._active_orders.values())
            combined.extend(self._history)

            normalised = [
                _normalise_order_record(order)
                for order in combined
            ]

            normalised.sort(
                key=lambda item: item.get("updated_at")
                or item.get("requested_at")
                or "",
                reverse=True,
            )

            def _record_timestamp(record: Mapping[str, Any]) -> datetime | None:
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
                        if threshold_tzinfo is not None:
                            parsed = parsed.replace(tzinfo=threshold_tzinfo)
                        else:
                            parsed = parsed.replace(tzinfo=timezone.utc)
                    elif threshold_tzinfo is not None:
                        parsed = parsed.astimezone(threshold_tzinfo)
                    return parsed
                return None

            filtered_orders: list[dict[str, Any]] = []
            for order in normalised:
                order_timestamp = _record_timestamp(order)
                if order_timestamp is None or order_timestamp >= start_dt:
                    filtered_orders.append(order)

            total = len(filtered_orders)
            if limit:
                sliced = filtered_orders[offset : offset + limit]
            else:
                sliced = filtered_orders[offset:]
            updated_at = self._last_updated_at
            last_event_id = self._last_message_id or ""
            page = offset // limit if limit else 0

        start_iso = _serialise_datetime(start_dt)
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

    def _build_repository_order_payload(
        self,
        record: Mapping[str, Any],
        *,
        timestamp: str | datetime | None = None,
    ) -> Mapping[str, Any]:
        payload = _normalise_cached_record(record)
        payload.pop("id", None)
        if timestamp is not None:
            normalised_timestamp = _normalise_timestamp(timestamp)
            if normalised_timestamp is not None:
                payload["timestamp"] = normalised_timestamp
            elif isinstance(timestamp, datetime):
                payload["timestamp"] = _serialise_datetime(timestamp)
            else:
                payload["timestamp"] = str(timestamp)
        return payload

    def _record_repository_failure(self, message: str) -> None:
        self._repository_failure_count += 1
        self._repository_last_error = message

    async def _upsert_order_snapshot(
        self,
        record: Mapping[str, Any],
        *,
        timestamp: str | datetime | None = None,
    ) -> int | None:
        if self.order_repository_factory is None:
            return None

        repository = self._order_repository
        if repository is None:
            self.logger.warning("Order repository unavailable; dropping snapshot")
            self._record_repository_failure("repository unavailable")
            return None

        payload = self._build_repository_order_payload(record, timestamp=timestamp)

        try:
            result = await repository.upsert_order(payload)
        except Exception as exc:
            self._record_repository_failure(str(exc))
            self.logger.warning(
                "Failed to persist order snapshot to repository", exc_info=True
            )
            return None
        else:
            self._repository_last_error = ""
            return result

    async def _register_persisted_order_id(
        self,
        record_identifier: str,
        snapshot: Mapping[str, Any],
        inserted_id: int,
    ) -> None:
        identifier = str(record_identifier or "").strip()
        if not identifier:
            return
        inserted_id_str = str(inserted_id)
        command_id = _coerce_str(snapshot.get("command_id"))
        order_id = _coerce_str(snapshot.get("order_id"))

        async with self._lock:
            record = self._active_orders.get(identifier)
            if record is not None:
                record["id"] = inserted_id
                if command_id:
                    record["command_id"] = command_id

            for entry in self._history:
                if _record_matches(entry, identifier, order_id, command_id):
                    entry["id"] = inserted_id
                    if command_id:
                        entry["command_id"] = command_id

            self._command_to_order[identifier] = identifier
            self._command_to_order[inserted_id_str] = identifier
            if command_id:
                self._command_to_order[command_id] = identifier
            if order_id:
                self._command_to_order[order_id] = identifier

    async def _initialise_order_repository(self) -> None:
        if self.order_repository_factory is None:
            return
        try:
            created = self.order_repository_factory()
            repository = await _maybe_await(created)
        except Exception as exc:
            self._order_repository = None
            self._order_repository_close = None
            self._record_repository_failure(str(exc))
            self.logger.exception("Failed to initialise order repository")
            return

        self._order_repository = repository

        close_candidate = getattr(repository, "aclose", None)
        if callable(close_candidate):

            async def _async_close() -> None:
                await _maybe_await(close_candidate())

            self._order_repository_close = _async_close
        else:
            close_candidate = getattr(repository, "close", None)
            if callable(close_candidate):

                async def _sync_close() -> None:
                    await _maybe_await(close_candidate())

                self._order_repository_close = _sync_close
            else:
                self._order_repository_close = None

        self._repository_last_error = ""

    async def _shutdown_order_repository(self) -> None:
        close_callable = self._order_repository_close
        repository = self._order_repository
        self._order_repository_close = None
        self._order_repository = None
        if close_callable is not None:
            try:
                await close_callable()
            except Exception:  # pragma: no cover - defensive guard
                self.logger.debug("Failed to close order repository", exc_info=True)
            return
        if repository is not None:
            close_candidate = getattr(repository, "close", None)
            if callable(close_candidate):
                try:
                    await _maybe_await(close_candidate())
                except Exception:  # pragma: no cover - defensive guard
                    self.logger.debug(
                        "Failed to close order repository", exc_info=True
                    )

    async def _publish_health(self) -> None:
        if self._redis is None:
            return
        async with self._lock:
            metrics = {
                "active_orders": len(self._active_orders),
                "last_event_id": self._last_message_id or "",
                "updated_at": _serialise_datetime(self._last_updated_at),
            }
            if self.order_repository_factory is not None:
                metrics["repository_failures"] = self._repository_failure_count
                metrics["repository_last_error"] = self._repository_last_error
                metrics["repository_available"] = bool(self._order_repository)
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
        record_identifier_for_repo: str | None = None
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
            record_identifier_for_repo = record_identifier
            updated = True
        if record_snapshot is not None:
            inserted_id = await self._upsert_order_snapshot(
                record_snapshot, timestamp=event_time
            )
            if inserted_id is not None and record_identifier_for_repo is not None:
                await self._register_persisted_order_id(
                    record_identifier_for_repo, record_snapshot, inserted_id
                )
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
    runtime_instance: OrdersRuntime | None = runtime

    def _initialise_runtime() -> OrdersRuntime:
        if runtime is not None:
            return runtime

        try:
            from src.db.bootstrap import SchemaBootstrapError, ensure_schema
        except ImportError:  # pragma: no cover - optional dependency in minimal builds
            LOGGER.debug("Database bootstrap helper unavailable; skipping schema check")
        else:
            try:
                ensure_schema(logger=LOGGER)
            except SchemaBootstrapError:
                LOGGER.exception("Database schema bootstrap failed")
            except Exception as exc:  # pragma: no cover - defensive logging
                LOGGER.warning("Database schema bootstrap skipped: %s", exc, exc_info=True)

        try:
            from src.db import order_repository
        except ImportError as exc:  # pragma: no cover - optional dependency
            LOGGER.info("Orders repository unavailable: %s", exc)
            return OrdersRuntime()

        def _default_order_repository() -> OrderRepositoryProtocol:
            repository = order_repository()

            upsert_callable = getattr(repository, "upsert_order", None)
            if not callable(upsert_callable):
                return repository

            if inspect.iscoroutinefunction(upsert_callable):
                return repository

            try:
                from src.db import run_in_thread as _run_in_thread
            except ImportError:  # pragma: no cover - optional dependency
                async def _run_in_thread(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
                    return await asyncio.to_thread(func, *args, **kwargs)

            class _AsyncOrderRepository(OrderRepositoryProtocol):
                __slots__ = ("_repository", "_upsert", "_close")

                def __init__(self, repo: Any) -> None:
                    self._repository = repo
                    self._upsert = getattr(repo, "upsert_order")
                    self._close = getattr(repo, "close", None)

                async def upsert_order(self, payload: Mapping[str, Any]) -> int | None:
                    return await _run_in_thread(self._upsert, payload)

                async def aclose(self) -> None:
                    if callable(self._close):
                        await _run_in_thread(self._close)

                async def close(self) -> None:  # pragma: no cover - compatibility shim
                    await self.aclose()

                def __getattr__(self, name: str) -> Any:
                    return getattr(self._repository, name)

            return _AsyncOrderRepository(repository)

        return OrdersRuntime(order_repository_factory=_default_order_repository)

    def _get_or_create_runtime() -> OrdersRuntime:
        nonlocal runtime_instance
        if runtime_instance is None:
            runtime_instance = _initialise_runtime()
        return runtime_instance

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        instance = _get_or_create_runtime()
        await instance.start()
        try:
            yield
        finally:
            await instance.stop()

    app = FastAPI(
        title="Orders Service",
        version="1.0.0",
        lifespan=_lifespan if enable_events else None,
    )

    app.state.get_orders_runtime = _get_or_create_runtime

    async def get_runtime() -> OrdersRuntime:
        return _get_or_create_runtime()

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

    @app.patch("/orders/{order_id}/fills", status_code=status.HTTP_200_OK)
    async def update_order_fills(
        order_id: str,
        request: FillMetricsUpdate,
        runtime: OrdersRuntime = Depends(get_runtime),
    ) -> Mapping[str, Any]:
        payload = request.model_dump(exclude_none=True)
        try:
            record = await runtime.update_fill_metrics(order_id, payload)
        except KeyError:
            raise HTTPException(status_code=404, detail="order not found")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"success": True, "order": _normalise_order_record(record)}

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
