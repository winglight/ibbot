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

from src.db.repositories.order import OrderNotFoundError
from src.ibbot import update_service_health
from src.ibbot.redis import DEFAULT_COMMAND_QUEUE, enqueue_ib_command, get_redis_client


@runtime_checkable
class OrderRepositoryProtocol(Protocol):
    async def insert_order(self, payload: Mapping[str, Any]) -> int:
        ...

    async def update_order(self, payload: Mapping[str, Any]) -> None:
        ...

    async def upsert_order(
        self, payload: Mapping[str, Any], *, allow_insert: bool = True
    ) -> int | None:
        ...

    async def fetch_recent_orders(self, *, limit: int) -> list[Mapping[str, Any]]:
        ...

    async def paginate_orders(
        self,
        *,
        offset: int,
        limit: int,
        start_date: datetime | None = None,
    ) -> tuple[list[Mapping[str, Any]], int]:
        ...

    async def fetch_order(self, identifier: str) -> Mapping[str, Any] | None:
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


def _looks_like_uuid(value: Any) -> bool:
    if value is None:
        return False
    text = str(value).strip()
    if not text:
        return False
    try:
        uuid.UUID(text)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


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


# -- data classes -----------------------------------------------------------------


@dataclass
class OrderPersistenceResult:
    inserted_id: int | None = None
    not_found: bool = False


def _is_order_not_found_error(exc: BaseException) -> bool:
    return isinstance(exc, OrderNotFoundError) or exc.__class__.__name__ == "OrderNotFoundError"


@dataclass
class OrdersRuntime:
    redis_factory: Callable[[], Awaitable[Any]] = get_redis_client
    order_repository_factory: (
        Callable[[], Awaitable[OrderRepositoryProtocol] | OrderRepositoryProtocol]
        | None
    ) = None
    command_queue: str = DEFAULT_COMMAND_QUEUE
    results_stream: str = "stream:ib:results"
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
    _ib_only_orders: set[str] = field(default_factory=set, init=False, repr=False)
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
        self._ib_only_orders = set()

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
            await self._hydrate_from_repository()
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
            entry_snapshot = _clone_order_record(entry)
        if entry_snapshot is not None:
            persist_result = await self._persist_order_snapshot(
                entry_snapshot,
                timestamp=entry_snapshot.get("requested_at"),
                mode="insert",
            )
            if persist_result.inserted_id is not None:
                await self._register_persisted_order_id(
                    command_id,
                    entry_snapshot,
                    persist_result,
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

        if record_snapshot is not None:
            persist_result = await self._persist_order_snapshot(
                record_snapshot,
                timestamp=timestamp,
                mode="update",
            )
            if (
                persist_result.inserted_id is not None
                and record_identifier_for_repo is not None
            ):
                await self._register_persisted_order_id(
                    record_identifier_for_repo,
                    record_snapshot,
                    persist_result,
                )
            elif persist_result.not_found:
                identifier_for_log = (
                    record_identifier_for_repo
                    or _coerce_str(record_snapshot.get("command_id"))
                    or _coerce_str(record_snapshot.get("order_id"))
                    or "unknown"
                )
                self.logger.warning(
                    "Order repository missing order %s during fill metrics update",
                    identifier_for_log,
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

        def _record_identity(
            record: Mapping[str, Any], fallback: str | None = None
        ) -> tuple[str, ...]:
            identifiers = {
                _coerce_str(record.get("id")),
                _coerce_str(record.get("order_id")),
                _coerce_str(record.get("command_id")),
                _coerce_str(record.get("client_order_id")),
                _coerce_str(record.get("ib_order_id")),
                _coerce_str(fallback),
            }
            filtered = sorted(value for value in identifiers if value)
            if filtered:
                return tuple(filtered)
            return (str(id(record)),)

        async with self._lock:
            in_memory_records: list[
                tuple[tuple[str, ...], dict[str, Any], datetime | None]
            ] = []
            for identifier, entry in self._active_orders.items():
                normalised = _normalise_order_record(entry)
                identity = _record_identity(normalised, fallback=identifier)
                timestamp = _record_timestamp(normalised)
                if timestamp is None or timestamp >= start_dt:
                    in_memory_records.append((identity, normalised, timestamp))
            for entry in self._history:
                normalised = _normalise_order_record(entry)
                identity = _record_identity(normalised)
                timestamp = _record_timestamp(normalised)
                if timestamp is None or timestamp >= start_dt:
                    in_memory_records.append((identity, normalised, timestamp))
            updated_at = self._last_updated_at
            last_event_id = self._last_message_id or ""

        repository_records: list[
            tuple[tuple[str, ...], dict[str, Any], datetime | None]
        ] = []
        repository_total = len(in_memory_records)
        repository = self._order_repository
        if repository is not None and limit:
            try:
                raw_records, repository_total = await repository.paginate_orders(
                    offset=offset,
                    limit=limit,
                    start_date=start_dt,
                )
            except Exception as exc:  # pragma: no cover - defensive logging
                self._record_repository_failure(str(exc))
            else:
                repository_records = []
                for item in raw_records:
                    if not isinstance(item, Mapping):
                        continue
                    normalised = _normalise_cached_record(item)
                    identity = _record_identity(normalised)
                    timestamp = _record_timestamp(normalised)
                    repository_records.append((identity, normalised, timestamp))
                self._repository_last_error = ""

        combined: dict[
            tuple[str, ...], tuple[dict[str, Any], datetime | None]
        ] = {}
        for identity, record, timestamp in repository_records:
            combined[identity] = (record, timestamp)
        for identity, record, timestamp in in_memory_records:
            combined[identity] = (record, timestamp)

        def _sort_key(
            entry: tuple[tuple[str, ...], tuple[dict[str, Any], datetime | None]]
        ) -> datetime:
            timestamp = entry[1][1]
            if timestamp is None:
                return datetime.min.replace(tzinfo=timezone.utc)
            return timestamp

        combined_items = list(combined.items())
        combined_items.sort(key=_sort_key, reverse=True)
        ordered_records = [payload for _, (payload, _) in combined_items]

        total_records = max(repository_total, len(ordered_records))
        if limit:
            sliced = ordered_records[offset : offset + limit]
            page = offset // limit if limit else 0
        else:
            sliced = ordered_records[offset:]
            page = 0

        start_iso = _serialise_datetime(start_dt)
        return {
            "orders": sliced,
            "total": total_records,
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
        if record is None and self._order_repository is not None:
            repository_record: Mapping[str, Any] | None = None
            try:
                repository_record = await self._order_repository.fetch_order(identifier)
            except Exception as exc:  # pragma: no cover - defensive logging
                self._record_repository_failure(str(exc))
            else:
                if repository_record is not None:
                    self._repository_last_error = ""
            if repository_record is not None:
                normalised_repository_record = _normalise_cached_record(repository_record)
                record = normalised_repository_record
                status_value = _coerce_str(normalised_repository_record.get("status")) or ""
                canonical_identifier = (
                    _coerce_str(normalised_repository_record.get("command_id"))
                    or _coerce_str(normalised_repository_record.get("order_id"))
                    or _coerce_str(normalised_repository_record.get("id"))
                    or identifier
                )
                snapshot = _clone_order_record(normalised_repository_record)
                async with self._lock:
                    existing = self._find_order_unlocked(identifier)
                    if existing is None:
                        if status_value.lower() in _TERMINAL_STATUSES:
                            self._history.appendleft(snapshot)
                        else:
                            self._active_orders[canonical_identifier] = snapshot
                        if canonical_identifier:
                            self._command_to_order[canonical_identifier] = canonical_identifier
                        if snapshot.get("command_id"):
                            self._command_to_order[str(snapshot["command_id"])] = canonical_identifier
                        if snapshot.get("order_id"):
                            self._command_to_order[str(snapshot["order_id"])] = canonical_identifier
                        if snapshot.get("id"):
                            self._command_to_order[str(snapshot["id"])] = canonical_identifier
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

    async def _hydrate_from_repository(self) -> None:
        repository = self._order_repository
        if repository is None:
            async with self._lock:
                self._active_orders = {}
                self._history = deque(maxlen=self.history_limit)
                self._command_to_order = {}
                self._ib_only_orders = set()
                self._last_updated_at = None
            return

        fetch_limit = max(self.history_limit + 50, self.history_limit)
        try:
            records = await repository.fetch_recent_orders(limit=fetch_limit)
        except Exception as exc:  # pragma: no cover - defensive logging
            self._record_repository_failure(str(exc))
            return

        active_items: list[dict[str, Any]] = []
        history_items: list[dict[str, Any]] = []
        for item in records:
            if not isinstance(item, Mapping):
                continue
            record = _normalise_cached_record(item)
            status_value = _coerce_str(record.get("status")) or ""
            if status_value.lower() in _TERMINAL_STATUSES:
                history_items.append(record)
            else:
                active_items.append(record)

        async with self._lock:
            self._active_orders = {}
            self._history = deque(maxlen=self.history_limit)
            self._command_to_order = {}
            self._ib_only_orders = set()
            latest_timestamp: datetime | None = None

            def _register_timestamp(candidate: Mapping[str, Any]) -> None:
                nonlocal latest_timestamp
                for key in (
                    "updated_at",
                    "submitted_at",
                    "requested_at",
                    "completed_at",
                    "created_at",
                    "timestamp",
                ):
                    value = candidate.get(key)
                    parsed = _parse_datetime(value)
                    if parsed is None:
                        continue
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    if latest_timestamp is None or parsed > latest_timestamp:
                        latest_timestamp = parsed
                    break

            for record in active_items:
                identifier = str(
                    record.get("id")
                    or record.get("order_id")
                    or record.get("command_id")
                    or record.get("ib_order_id")
                    or ""
                )
                if not identifier:
                    continue
                command_id_value = _coerce_str(record.get("command_id"))
                order_id_value = _coerce_str(record.get("order_id"))
                if (
                    command_id_value
                    and order_id_value
                    and command_id_value == order_id_value
                    and not _looks_like_uuid(command_id_value)
                ):
                    self._ib_only_orders.add(command_id_value)
                    self.logger.info(
                        "Dropping IB-only order %s from repository hydration",
                        command_id_value,
                    )
                    continue
                record.setdefault("command_id", identifier)
                self._active_orders[identifier] = record
                if record.get("command_id"):
                    self._command_to_order[str(record["command_id"])] = identifier
                if record.get("order_id"):
                    self._command_to_order[str(record["order_id"])] = identifier
                if record.get("id"):
                    self._command_to_order[str(record["id"])] = identifier
                _register_timestamp(record)

            for record in history_items:
                identifier = str(
                    record.get("id")
                    or record.get("order_id")
                    or record.get("command_id")
                    or ""
                )
                command_id_value = _coerce_str(record.get("command_id"))
                order_id_value = _coerce_str(record.get("order_id"))
                if (
                    command_id_value
                    and order_id_value
                    and command_id_value == order_id_value
                    and not _looks_like_uuid(command_id_value)
                ):
                    self._ib_only_orders.add(command_id_value)
                    self.logger.info(
                        "Dropping IB-only historical order %s from repository hydration",
                        command_id_value,
                    )
                    continue
                if identifier:
                    self._command_to_order[str(record.get("command_id", identifier))] = identifier
                if record.get("order_id"):
                    self._command_to_order[str(record["order_id"])] = identifier
                if record.get("id"):
                    self._command_to_order[str(record["id"])] = identifier
                self._history.appendleft(record)
                _register_timestamp(record)

            self._last_updated_at = latest_timestamp
            if not self._last_message_id:
                self._last_message_id = ""
            self._repository_last_error = ""

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

    async def _persist_order_snapshot(
        self,
        record: Mapping[str, Any],
        *,
        timestamp: str | datetime | None = None,
        mode: str = "insert",
    ) -> OrderPersistenceResult:
        if self.order_repository_factory is None:
            return OrderPersistenceResult()

        repository = self._order_repository
        if repository is None:
            self.logger.warning("Order repository unavailable; dropping snapshot")
            self._record_repository_failure("repository unavailable")
            return OrderPersistenceResult()

        payload = self._build_repository_order_payload(record, timestamp=timestamp)

        try:
            if mode == "insert":
                inserted_id = await repository.insert_order(payload)
                result = OrderPersistenceResult(inserted_id=inserted_id)
            elif mode == "update":
                await repository.update_order(payload)
                result = OrderPersistenceResult()
            else:  # pragma: no cover - defensive programming
                raise ValueError(f"Unsupported persistence mode: {mode}")
        except Exception as exc:
            if _is_order_not_found_error(exc):
                message = str(exc) or "order not found"
                self._record_repository_failure(message)
                return OrderPersistenceResult(not_found=True)
            self._record_repository_failure(str(exc))
            self.logger.warning(
                "Failed to persist order snapshot to repository", exc_info=True
            )
            return OrderPersistenceResult()
        else:
            self._repository_last_error = ""
            return result

    async def _register_persisted_order_id(
        self,
        record_identifier: str,
        snapshot: Mapping[str, Any],
        result: OrderPersistenceResult,
    ) -> None:
        inserted_id = result.inserted_id
        if inserted_id is None:
            return
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
            self._ib_only_orders.discard(identifier)

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
        matched_existing = False
        skip_ib_only_event = False
        ib_only_identifier: str | None = None
        ib_only_first_seen = False
        async with self._lock:
            record_identifier = identifier
            record = self._active_orders.get(record_identifier)
            if record is not None:
                matched_existing = True
            from_history = False
            if record is None and command_id and command_id in self._command_to_order:
                record_identifier = self._command_to_order[command_id]
                record = self._active_orders.get(record_identifier)
                if record is not None:
                    matched_existing = True
            if record is None and order_id and order_id in self._command_to_order:
                record_identifier = self._command_to_order[order_id]
                record = self._active_orders.get(record_identifier)
                if record is not None:
                    matched_existing = True
            if record is None:
                for item in self._history:
                    if _record_matches(item, identifier, order_id, command_id):
                        record = item
                        record_identifier = str(item.get("id") or identifier)
                        from_history = True
                        matched_existing = True
                        break
            if record is None and not command_id:
                skip_ib_only_event = True
                ib_only_identifier = record_identifier
                ib_only_first_seen = record_identifier not in self._ib_only_orders
                self._ib_only_orders.add(record_identifier)
                self._active_orders.pop(record_identifier, None)
                aliases_to_remove = {record_identifier}
                if order_id:
                    aliases_to_remove.add(order_id)
                if command_id:
                    aliases_to_remove.add(command_id)
                for alias in list(aliases_to_remove):
                    if alias:
                        self._command_to_order.pop(alias, None)
                if self._history:
                    retained_history = [
                        item
                        for item in self._history
                        if not _record_matches(item, record_identifier, order_id, command_id)
                    ]
                    if len(retained_history) != len(self._history):
                        self._history = deque(retained_history, maxlen=self.history_limit)
            if skip_ib_only_event:
                record = None
            else:
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
                self._ib_only_orders.discard(record_identifier)
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
                        "type": parsed_payload.get("type")
                        or parsed_payload.get("category")
                        or "event",
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
                if status_lower in _TERMINAL_STATUSES:
                    self._ib_only_orders.discard(record_identifier)
                updated = True
        if skip_ib_only_event:
            if ib_only_first_seen:
                identifier_for_log = ib_only_identifier or order_id or command_id or "unknown"
                self.logger.warning(
                    "Ignoring IB-only order event without command_id: %s",
                    identifier_for_log,
                )
            return False
        if record_snapshot is not None:
            persist_result = await self._persist_order_snapshot(
                record_snapshot,
                timestamp=event_time,
                mode="update",
            )
            if (
                persist_result.inserted_id is not None
                and record_identifier_for_repo is not None
            ):
                await self._register_persisted_order_id(
                    record_identifier_for_repo,
                    record_snapshot,
                    persist_result,
                )
            elif persist_result.not_found:
                identifier_for_log = (
                    record_identifier_for_repo
                    or _coerce_str(record_snapshot.get("order_id"))
                    or _coerce_str(record_snapshot.get("ib_order_id"))
                    or _coerce_str(record_snapshot.get("command_id"))
                    or "unknown"
                )
                self.logger.warning(
                    "Repository missing persisted order %s for update; skipping insert",
                    identifier_for_log,
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

            method_names = ("insert_order", "update_order", "upsert_order")
            method_map: dict[str, Callable[..., Any]] = {}
            for name in method_names:
                candidate = getattr(repository, name, None)
                if callable(candidate):
                    method_map[name] = candidate

            has_sync_methods = any(
                not inspect.iscoroutinefunction(callable_candidate)
                for callable_candidate in method_map.values()
            )

            if not has_sync_methods:
                return repository

            try:
                from src.db import run_in_thread as _run_in_thread
            except ImportError:  # pragma: no cover - optional dependency
                async def _run_in_thread(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
                    return await asyncio.to_thread(func, *args, **kwargs)

            class _AsyncOrderRepository(OrderRepositoryProtocol):
                __slots__ = (
                    "_repository",
                    "_insert_sync",
                    "_insert_async",
                    "_update_sync",
                    "_update_async",
                    "_upsert_sync",
                    "_upsert_async",
                    "_close_sync",
                    "_close_async",
                )

                def __init__(self, repo: Any) -> None:
                    self._repository = repo
                    insert_candidate = getattr(repo, "insert_order", None)
                    if callable(insert_candidate) and inspect.iscoroutinefunction(
                        insert_candidate
                    ):
                        self._insert_async = insert_candidate
                        self._insert_sync = None
                    elif callable(insert_candidate):
                        self._insert_sync = insert_candidate
                        self._insert_async = None
                    else:
                        self._insert_sync = None
                        self._insert_async = None

                    update_candidate = getattr(repo, "update_order", None)
                    if callable(update_candidate) and inspect.iscoroutinefunction(
                        update_candidate
                    ):
                        self._update_async = update_candidate
                        self._update_sync = None
                    elif callable(update_candidate):
                        self._update_sync = update_candidate
                        self._update_async = None
                    else:
                        self._update_sync = None
                        self._update_async = None

                    upsert_candidate = getattr(repo, "upsert_order", None)
                    if callable(upsert_candidate) and inspect.iscoroutinefunction(
                        upsert_candidate
                    ):
                        self._upsert_async = upsert_candidate
                        self._upsert_sync = None
                    elif callable(upsert_candidate):
                        self._upsert_sync = upsert_candidate
                        self._upsert_async = None
                    else:
                        self._upsert_sync = None
                        self._upsert_async = None

                    close_candidate = getattr(repo, "close", None)
                    if callable(close_candidate) and inspect.iscoroutinefunction(
                        close_candidate
                    ):
                        self._close_async = close_candidate
                        self._close_sync = None
                    elif callable(close_candidate):
                        self._close_sync = close_candidate
                        self._close_async = None
                    else:
                        self._close_sync = None
                        self._close_async = None

                async def insert_order(self, payload: Mapping[str, Any]) -> int:
                    if self._insert_async is not None:
                        return await self._insert_async(payload)
                    if self._insert_sync is not None:
                        return await _run_in_thread(self._insert_sync, payload)
                    insert_callable = getattr(self._repository, "insert_order", None)
                    if not callable(insert_callable):
                        raise AttributeError("insert_order")
                    result = insert_callable(payload)
                    return await _maybe_await(result)

                async def update_order(self, payload: Mapping[str, Any]) -> None:
                    if self._update_async is not None:
                        await self._update_async(payload)
                        return
                    if self._update_sync is not None:
                        await _run_in_thread(self._update_sync, payload)
                        return
                    update_callable = getattr(self._repository, "update_order", None)
                    if not callable(update_callable):
                        raise AttributeError("update_order")
                    await _maybe_await(update_callable(payload))

                async def upsert_order(
                    self, payload: Mapping[str, Any], *, allow_insert: bool = True
                ) -> int | None:
                    if self._upsert_async is not None:
                        return await self._upsert_async(
                            payload, allow_insert=allow_insert
                        )
                    if self._upsert_sync is not None:
                        return await _run_in_thread(
                            self._upsert_sync, payload, allow_insert=allow_insert
                        )
                    try:
                        await self.update_order(payload)
                    except Exception as exc:
                        if exc.__class__.__name__ != "OrderNotFoundError":
                            raise
                        if not allow_insert:
                            return None
                        return await self.insert_order(payload)
                    return None

                async def aclose(self) -> None:
                    if self._close_async is not None:
                        await self._close_async()
                    elif self._close_sync is not None:
                        await _run_in_thread(self._close_sync)

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
