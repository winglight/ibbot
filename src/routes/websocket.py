import asyncio
import contextlib
import json
import math
import os
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Protocol,
    Tuple,
)

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from loguru import logger

import core
from src.account_snapshot_cache import (
    read_account_info_snapshot,
    read_positions_snapshot,
)
from src.account_snapshot_service import IB_COMMAND_SOURCE
from src.websocket_manager import websocket_manager as global_websocket_manager
from src.data_layer import (
    DataSubscriptionRequest,
    DataSubscriptionToken,
    DataSourceError,
    EventEnvelope,
    get_data_layer_mode,
    get_data_source_manager,
    is_legacy_mode,
)
from src.routes.dom import get_dom_service_client
from src.utils import now_local, parse_local_datetime, to_local_iso
from src.routes.logs import load_logs_data
from src.routes.monitoring import build_metric_series_payload, get_queue_metrics
from src.service_status import build_service_status_payload
from src.ib_command_health import (
    IB_COMMAND_HEALTH_KEY,
    IB_COMMAND_HEALTH_STALE_AFTER_SEC,
    build_ib_connection_health_payload,
    resolve_ib_account_type,
)
from src.routes.service_clients import (
    AccountServiceClient,
    AccountServiceResult,
    MarketDataServiceClient,
    OrdersServiceClient,
    RiskServiceClient,
    StrategyServiceClient,
    ServiceClientError,
    ServiceUnavailableError,
)
from services.risk.engine import RiskRule
from src.ibbot.redis import PubSubListener, get_redis_client
from src.utils.strategy_cache import (
    compute_performance_metrics as _compute_performance_metrics,
    get_strategy_stats as _get_strategy_stats,
)

router = APIRouter()

RATE_LIMIT_SECONDS = 15.0
SERVICE_UNAVAILABLE_MESSAGE = "服务不可用"
RISK_RULES_UPDATE_CHANNEL = "pubsub:risk:rules:update"
_MARKET_SNAPSHOT_LOG_INTERVAL = 30.0

_ACCOUNT_CLIENT = AccountServiceClient()
_MARKET_CLIENT = MarketDataServiceClient()
_ORDERS_CLIENT = OrdersServiceClient()
_RISK_CLIENT = RiskServiceClient()
_STRATEGY_CLIENT = StrategyServiceClient()


def _services():
    return core.get_container()


def _websocket_manager():
    services = _services()
    manager = getattr(services, "websocket_manager", None) if services else None
    if manager:
        return manager
    return global_websocket_manager


def _service_unavailable_payload(payload_type: str, **fields: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "type": payload_type,
        "success": False,
        "error": SERVICE_UNAVAILABLE_MESSAGE,
    }
    payload.update(fields)
    return payload


def _extract_service_unavailable_payload(
    value: Any, payload_type: str
) -> Optional[Dict[str, Any]]:
    if not isinstance(value, Mapping):
        return None

    try:
        payload = dict(value)
    except Exception:
        return None

    if (
        payload.get("type") == payload_type
        and payload.get("error") == SERVICE_UNAVAILABLE_MESSAGE
        and payload.get("success") is False
    ):
        return payload

    return None


class ConnectionContext(NamedTuple):
    health_payload: Optional[Dict[str, Any]]
    connected: bool
    account_type: str
    last_error: Optional[str]
    redis_connected: Optional[bool]
    redis_fresh: bool
    health_error: Optional[str]
    error_code: Optional[str]


_METADATA_KEYS = tuple(
    dict.fromkeys(
        list(AccountServiceClient._METADATA_KEYS)
        + ["fast", "force_refresh", "requested_fast", "requested_force_refresh"]
    )
)


def _merge_metadata(*sources: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        for key in _METADATA_KEYS:
            value = source.get(key)
            if value is None:
                continue
            merged[key] = value
    if "snapshot_updated_at" not in merged and isinstance(merged.get("updated_at"), str):
        merged["snapshot_updated_at"] = merged["updated_at"]
    return merged


async def cache_get_strategy_stats(name: str) -> Dict[str, Any]:
    try:
        stats = await _get_strategy_stats(name)
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.debug("Failed to load cached strategy stats", name=name, error=str(exc))
        return {}
    return stats or {}


async def cache_compute_performance(
    name: str,
    *,
    range_value: str | None = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
) -> Dict[str, Any]:
    try:
        metrics = await _compute_performance_metrics(
            name,
            start_date=start_date,
            end_date=end_date,
        )
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.debug("Failed to compute cached strategy metrics", name=name, error=str(exc))
        return {}
    return metrics or {}


def _extract_positions_payload(
    result: Any,
) -> Tuple[Optional[List[Any]], Dict[str, Any]]:
    """Normalize positions payload and extract associated metadata."""

    metadata: Dict[str, Any] = {}

    if isinstance(result, AccountServiceResult):
        metadata = {
            key: value
            for key, value in result.metadata.items()
            if key in _METADATA_KEYS and value is not None
        }
        data = result.data
        if isinstance(data, list):
            normalized = [
                dict(item) if isinstance(item, Mapping) else item for item in data
            ]
            return normalized, metadata
        return None, metadata

    if isinstance(result, Mapping):
        metadata = {
            key: value
            for key, value in result.items()
            if key in _METADATA_KEYS and value is not None
        }
        raw_positions = result.get("positions") or result.get("data")
        if isinstance(raw_positions, list):
            normalized = [
                dict(item) if isinstance(item, Mapping) else item
                for item in raw_positions
            ]
            return normalized, metadata
        return None, metadata

    if isinstance(result, list):
        normalized = [dict(item) if isinstance(item, Mapping) else item for item in result]
        return normalized, metadata

    return None, metadata


_LAST_MARKET_SNAPSHOT_LOG: Dict[str, float] = {}


def _log_market_snapshot_request(symbol: str) -> None:
    now = time.monotonic()
    last_logged_at = _LAST_MARKET_SNAPSHOT_LOG.get(symbol)
    if last_logged_at is None or now - last_logged_at >= _MARKET_SNAPSHOT_LOG_INTERVAL:
        _LAST_MARKET_SNAPSHOT_LOG[symbol] = now
        logger.debug("Fetching market snapshot via service for {}", symbol)


async def _fetch_market_snapshot(symbol: str) -> Optional[Dict[str, Any]]:
    _log_market_snapshot_request(symbol)
    try:
        payload = await _MARKET_CLIENT.fetch_current_price(symbol)
    except ServiceUnavailableError as exc:
        logger.debug("Market data service unavailable for {}: {}", symbol, exc)
        return None
    except ServiceClientError as exc:
        logger.warning("Market data service error for {}: {}", symbol, exc)
        return None

    if isinstance(payload, Mapping):
        return dict(payload)
    return None


async def _fetch_account_info(
    *,
    force_refresh: bool = False,
    fast: bool = False,
) -> AccountServiceResult | Dict[str, Any] | None:
    try:
        payload = await _ACCOUNT_CLIENT.fetch_info(
            force_refresh=force_refresh,
            fast=fast,
        )
    except ServiceUnavailableError as exc:
        logger.debug("Account service unavailable: {}", exc)
        error_payload = _service_unavailable_payload("account_update")
        error_payload["details"] = str(exc)
        return error_payload
    except ServiceClientError as exc:
        logger.warning("Account service error: {}", exc)
        return None
    return payload


async def _fetch_positions(
    *, force_refresh: bool = False, fast: bool = True
) -> AccountServiceResult | Dict[str, Any] | None:
    try:
        payload = await _ACCOUNT_CLIENT.fetch_positions(
            force_refresh=force_refresh,
            fast=fast,
        )
    except ServiceUnavailableError as exc:
        logger.debug("Account service unavailable for positions: {}", exc)
        error_payload = _service_unavailable_payload("position_update")
        error_payload["details"] = str(exc)
        return error_payload
    except ServiceClientError as exc:
        logger.warning("Account service error fetching positions: {}", exc)
        return None

    if isinstance(payload, AccountServiceResult):
        return payload
    if isinstance(payload, list):
        return AccountServiceResult(
            data=[dict(item) if isinstance(item, Mapping) else item for item in payload],
            metadata={},
        )
    if isinstance(payload, Mapping):
        items = payload.get("positions")
        if items is None:
            items = payload.get("data")
        if isinstance(items, list):
            return AccountServiceResult(
                data=[dict(item) if isinstance(item, Mapping) else item for item in items],
                metadata={},
                raw=dict(payload),
            )
    return None


async def _fetch_orders(
    *,
    limit: int,
    offset: int,
    start_date: Optional[str] = None,
) -> Optional[Tuple[List[Dict[str, Any]], Optional[int], Dict[str, Any]]]:
    try:
        payload = await _ORDERS_CLIENT.list_orders(
            limit=limit,
            offset=offset,
            start_date=start_date,
        )
    except ServiceUnavailableError as exc:
        logger.debug("Orders service unavailable for orders: {}", exc)
        error_payload = _service_unavailable_payload("orders_snapshot")
        error_payload["details"] = str(exc)
        return error_payload
    except ServiceClientError as exc:
        logger.warning("Orders service error fetching orders: {}", exc)
        return None

    orders_list: List[Dict[str, Any]] = []
    total_count: Optional[int] = None
    metadata: Dict[str, Any] = {}
    if start_date:
        metadata["start_date"] = start_date

    if isinstance(payload, list):
        orders_list = [dict(item) if isinstance(item, Mapping) else {"raw": item} for item in payload]
    elif isinstance(payload, Mapping):
        candidates = None
        for key in ("orders", "data", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates = value
                break
        if candidates is None and isinstance(payload.get("order"), Mapping):
            candidates = [payload["order"]]
        if candidates is not None:
            orders_list = [dict(item) if isinstance(item, Mapping) else {"raw": item} for item in candidates]

        raw_total = payload.get("total") or payload.get("total_count")
        if raw_total is None:
            meta = payload.get("meta")
            if isinstance(meta, Mapping):
                raw_total = meta.get("total") or meta.get("total_count")
                start_meta = meta.get("start_date")
                if start_meta:
                    metadata["start_date"] = start_meta
                start_meta_tz = meta.get("start_date_timezone")
                if start_meta_tz:
                    metadata["start_date_timezone"] = start_meta_tz
        try:
            if raw_total is not None:
                total_count = int(raw_total)
        except (TypeError, ValueError):
            total_count = None

        payload_start = payload.get("start_date")
        if payload_start:
            metadata["start_date"] = payload_start
        payload_start_tz = payload.get("start_date_timezone")
        if payload_start_tz:
            metadata["start_date_timezone"] = payload_start_tz
        for key in ("limit", "offset", "page", "returned"):
            value = payload.get(key)
            if value is None:
                continue
            metadata[key] = value

    if not orders_list and isinstance(payload, Mapping):
        extra_orders = payload.get("orders")
        if isinstance(extra_orders, list):
            orders_list = [
                dict(item) if isinstance(item, Mapping) else {"raw": item}
                for item in extra_orders
            ]

    if not orders_list:
        orders_list = []

    return orders_list, total_count, metadata


def _decode_redis_mapping(raw: Dict[Any, Any]) -> Dict[str, str]:
    """Convert Redis hash payload into a plain string dictionary."""

    result: Dict[str, str] = {}
    for key, value in raw.items():
        if key is None:
            continue
        if isinstance(key, (bytes, bytearray)):
            key = key.decode("utf-8", errors="ignore")
        else:
            key = str(key)
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8", errors="ignore")
        elif value is None:
            value = ""
        else:
            value = str(value)
        result[key] = value
    return result


def _normalise_rules_payload(rules: Any) -> Any:
    if isinstance(rules, Mapping):
        normalised: Dict[str, Dict[str, Any]] = {}
        for symbol, value in rules.items():
            if isinstance(value, Mapping):
                payload = dict(value)
                symbol_raw = str(payload.get("symbol") or symbol)
                symbol_norm = symbol_raw.strip().upper() or symbol_raw
                payload.setdefault("symbol", symbol_norm)
                normalised[symbol_norm] = RiskRule.normalise_payload(payload)
            else:
                symbol_raw = str(symbol)
                symbol_norm = symbol_raw.strip().upper() or symbol_raw
                normalised[symbol_norm] = {"symbol": symbol_norm, "value": value}
        return normalised
    if isinstance(rules, list):
        normalised_list: List[Any] = []
        for item in rules:
            if isinstance(item, Mapping):
                payload = dict(item)
                symbol_raw = str(payload.get("symbol") or "")
                if symbol_raw:
                    payload["symbol"] = symbol_raw.strip().upper() or symbol_raw
                normalised_list.append(RiskRule.normalise_payload(payload))
            else:
                normalised_list.append(item)
        return normalised_list
    return rules


_risk_rules_listener: PubSubListener | None = None
_risk_rules_listener_started = False
_risk_rules_listener_lock = asyncio.Lock()


async def _handle_risk_rules_pubsub(_: str, message: str) -> None:
    try:
        data: Any = json.loads(message) if message else {}
    except Exception:
        logger.debug("Invalid risk rules pubsub payload: {}", message)
        data = {}

    payload: Dict[str, Any]
    if isinstance(data, Mapping):
        payload = dict(data)
    else:
        payload = {}

    if "rules" not in payload or not payload.get("rules"):
        try:
            service_snapshot = await _RISK_CLIENT.fetch_rules()
        except ServiceUnavailableError:
            service_snapshot = None
        except ServiceClientError:
            service_snapshot = None

        if isinstance(service_snapshot, Mapping):
            payload.setdefault(
                "rules",
                service_snapshot.get("rules") or service_snapshot.get("data"),
            )
            if service_snapshot.get("updated_at") is not None:
                payload.setdefault("updated_at", service_snapshot.get("updated_at"))
            payload.setdefault("source", "service")
        elif isinstance(service_snapshot, list):
            payload.setdefault("rules", service_snapshot)
            payload.setdefault("source", "service")

    rules_payload = payload.get("rules")
    if rules_payload is not None:
        normalised_rules = _normalise_rules_payload(rules_payload)
        payload["rules"] = normalised_rules
        if isinstance(normalised_rules, Mapping):
            payload["count"] = len(normalised_rules)
        elif "count" not in payload and isinstance(rules_payload, Mapping):
            payload["count"] = len(rules_payload)

    payload.setdefault("type", "risk_rules")
    payload.setdefault("success", True)
    payload.setdefault("source", "pubsub")

    await _websocket_manager().broadcast(payload)


async def _ensure_risk_rules_listener() -> None:
    global _risk_rules_listener_started, _risk_rules_listener
    if _risk_rules_listener_started:
        return

    async with _risk_rules_listener_lock:
        if _risk_rules_listener_started:
            return

        listener = PubSubListener()
        listener.register_callback(RISK_RULES_UPDATE_CHANNEL, _handle_risk_rules_pubsub)
        try:
            await listener.start()
        except Exception:
            logger.warning("Failed to start risk rules pubsub listener", exc_info=True)
            return

        _risk_rules_listener = listener
        _risk_rules_listener_started = True


async def _load_ib_command_health() -> Optional[Dict[str, Any]]:
    """Load ib-command service health details from Redis if available."""

    container = core.get_container()
    redis_client = getattr(container, "redis_client", None) if container else None
    if redis_client is None:
        return None

    try:
        raw = await redis_client.hgetall(IB_COMMAND_HEALTH_KEY)
    except Exception as exc:  # pragma: no cover - redis access errors
        logger.debug("Failed to load ib-command health from Redis: {}", exc)
        return None

    if not raw:
        return None

    stale_after = (
        IB_COMMAND_HEALTH_STALE_AFTER_SEC
        if IB_COMMAND_HEALTH_STALE_AFTER_SEC > 0
        else 30.0
    )

    return build_ib_connection_health_payload(
        _decode_redis_mapping(raw),
        stale_after=stale_after,
    )


async def _send_json(websocket: WebSocket, payload: Dict[str, Any]) -> bool:
    try:
        await websocket.send_text(json.dumps(payload))
        return True
    except WebSocketDisconnect:
        logger.debug("Websocket disconnected during send")
        return False
    except RuntimeError as exc:
        if "Cannot call \"send\" once a close message has been sent" in str(exc):
            logger.debug("Websocket closing; aborting further sends")
            return False
        raise


async def _send_or_disconnect(websocket: WebSocket, payload: Dict[str, Any]) -> None:
    if not await _send_json(websocket, payload):
        raise WebSocketDisconnect()


def _order_value(order: Any, key: str, default: Any = None) -> Any:
    if isinstance(order, Mapping):
        return order.get(key, default)
    return getattr(order, key, default)


def _serialise_order(order: Any) -> Dict[str, Any]:
    def _first_value(*keys: str) -> Any:
        for key in keys:
            value = _order_value(order, key)
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            return value
        return None

    def _coerce_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, bool):
            return float(value)
        if isinstance(value, (int, float)):
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

    raw_type = (_order_value(order, "order_type", "") or "").upper()
    if raw_type in ("MARKET", "MKT"):
        order_type = "MKT"
    elif raw_type in ("LIMIT", "LMT"):
        order_type = "LMT"
    else:
        order_type = raw_type or "MKT"

    limit_price_val = _coerce_float(_first_value("limit_price", "price"))
    if limit_price_val is None:
        limit_price_val = 0.0

    trigger_type = None
    try:
        if _order_value(order, "order_source", "manual") == "auto":
            rule_id = _order_value(order, "rule_id")
            symbol = _order_value(order, "symbol")
            if rule_id and rule_id != symbol:
                trigger_type = "strategy"
            else:
                trigger_type = "risk_rule"
    except Exception:
        trigger_type = None

    created_at = _first_value("submitted_at", "requested_at", "created_at")
    created_iso = None
    if isinstance(created_at, str):
        parsed_created = parse_local_datetime(created_at)
        if parsed_created is not None:
            created_iso = parsed_created.isoformat()
        else:
            try:
                created_iso = to_local_iso(created_at)  # type: ignore[arg-type]
            except Exception:
                created_iso = created_at
    else:
        try:
            created_iso = to_local_iso(created_at)
        except Exception:
            if created_at is not None:
                try:
                    created_iso = created_at.isoformat()
                except Exception:
                    created_iso = None

    avg_fill_price_val = _coerce_float(_first_value("avg_fill_price", "fill_price"))
    if avg_fill_price_val is None and order_type == "MKT" and limit_price_val > 0:
        avg_fill_price_val = limit_price_val

    filled_quantity = _coerce_float(_first_value("filled_quantity", "filled"))
    remaining_quantity = _coerce_float(_first_value("remaining_quantity", "remaining"))
    commission_val = _coerce_float(_first_value("commission"))

    order_source_val = _first_value("source", "order_source")
    strategy_val = _first_value("strategy", "strategy_name")
    notes_val = _first_value("notes", "note", "comment")
    if notes_val is None:
        metadata_val = _order_value(order, "metadata")
        if isinstance(metadata_val, Mapping):
            for key in ("notes", "note", "comment"):
                candidate = metadata_val.get(key)
                if candidate is None:
                    continue
                if isinstance(candidate, str) and not candidate.strip():
                    continue
                notes_val = candidate
                break

    return {
        "order_id": _order_value(order, "order_id"),
        "symbol": _order_value(order, "symbol"),
        "action": _order_value(order, "action"),
        "quantity": _order_value(order, "quantity"),
        "order_type": order_type,
        "limit_price":
            limit_price_val if order_type == "LMT" and limit_price_val > 0 else None,
        "avg_fill_price": avg_fill_price_val,
        "status": _order_value(order, "status"),
        "filled":
            filled_quantity
            if filled_quantity is not None
            else (
                _order_value(order, "quantity", 0)
                if _order_value(order, "status") == "Filled"
                else 0
            ),
        "remaining":
            remaining_quantity
            if remaining_quantity is not None
            else (
                0
                if _order_value(order, "status") == "Filled"
                else _order_value(order, "quantity", 0)
            ),
        "submit_time": created_iso,
        "order_source": order_source_val or "manual",
        "strategy_name": strategy_val,
        "trigger_type": trigger_type,
        "commission": commission_val,
        "pnl": _order_value(order, "pnl"),
        "notes": notes_val,
    }


def _orders_payload(
    orders: List[Any], *, limit: Optional[int] = None, start_date: Optional[str] = None,
    start_date_timezone: Optional[str] = None, page: Optional[int] = None,
    total: Optional[int] = None
) -> Dict[str, Any]:
    returned = len(orders)
    meta: Dict[str, Any] = {
        "limit": limit,
        "page_size": limit,
        "start_date": start_date,
        "start_date_timezone": start_date_timezone,
        "generated_at": to_local_iso(now_local()),
        "source": "websocket",
        "returned": returned,
    }
    if page is not None:
        meta["page"] = page
        meta["has_prev"] = page > 1
    if total is not None:
        meta["total"] = total
        if limit:
            total_pages = max(1, math.ceil(total / limit))
            meta["total_pages"] = total_pages
            if page is not None:
                meta["has_next"] = page < total_pages
                meta["has_prev"] = page > 1
        elif page is not None and "has_prev" not in meta:
            meta["has_prev"] = page > 1
    elif limit:
        if page is not None and "has_prev" not in meta:
            meta["has_prev"] = page > 1
        meta["has_next"] = returned >= limit

    return {
        "type": "orders_snapshot",
        "success": True,
        "orders": [_serialise_order(order) for order in orders],
        "meta": meta,
    }


def _allow_request(tracker: Dict[str, float], key: str, *, force: bool = False) -> bool:
    now = time.monotonic()
    if force:
        tracker[key] = now
        return True
    last = tracker.get(key, 0.0)
    if now - last >= RATE_LIMIT_SECONDS:
        tracker[key] = now
        return True
    return False


def _enrich_sizing_history_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return sizing history items unchanged.

    The risk microservice now returns fully hydrated entries so the websocket
    layer no longer needs to enrich the payload using the legacy SQLite
    database.  The helper is retained for backwards compatibility with the
    surrounding call sites but simply passes values through.
    """

    return items


@dataclass
class _PerformanceEntry:
    payload: Dict[str, Any]
    stored_at: float
    signature: str


class StrategyPerformanceCache:
    """Share recent strategy performance responses across websocket clients."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._entries: Dict[str, _PerformanceEntry] = {}
        ttl_raw = os.getenv("STRATEGY_PERFORMANCE_CACHE_TTL_SECONDS", "45")
        try:
            self._ttl = max(float(ttl_raw), 1.0)
        except (TypeError, ValueError):
            self._ttl = 45.0

    @staticmethod
    def _clone_payload(payload: Mapping[str, Any]) -> Dict[str, Any]:
        try:
            return deepcopy(payload)  # type: ignore[arg-type]
        except Exception:
            try:
                return dict(payload)
            except Exception:
                return {}

    @staticmethod
    def _clone_value(value: Any) -> Any:
        try:
            return deepcopy(value)
        except Exception:
            return value

    @staticmethod
    def _normalise_timestamp(value: Any) -> Any:
        if isinstance(value, datetime):
            return to_local_iso(value)
        return value

    @staticmethod
    def _coerce_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        return str(value).strip()

    @classmethod
    def _normalise_name(cls, value: str) -> str:
        return cls._coerce_text(value).lower()

    @classmethod
    def _normalise_range(cls, value: str | None) -> str:
        return cls._coerce_text(value or "").lower()

    @classmethod
    def _build_key(
        cls,
        name: str,
        range_value: str | None,
        start: str | None,
        end: str | None,
    ) -> str:
        return "::".join(
            (
                cls._normalise_name(name),
                cls._normalise_range(range_value),
                cls._coerce_text(start),
                cls._coerce_text(end),
            )
        )

    def _prune_expired(self, *, now: Optional[float] = None) -> None:
        now_value = now or time.monotonic()
        if not self._entries:
            return
        expired: List[str] = []
        for key, entry in self._entries.items():
            if now_value - entry.stored_at > self._ttl:
                expired.append(key)
        for key in expired:
            self._entries.pop(key, None)

    @staticmethod
    def _compute_signature(payload: Mapping[str, Any]) -> str:
        try:
            return json.dumps(payload, sort_keys=True, ensure_ascii=False)
        except TypeError:
            try:
                serialisable = json.loads(json.dumps(payload, default=str))
            except Exception:
                serialisable = {
                    str(key): str(value) for key, value in payload.items()
                }
            if isinstance(serialisable, Mapping):
                serialisable = dict(serialisable)
            return json.dumps(serialisable, sort_keys=True, ensure_ascii=False)

    async def store_message(
        self,
        name: str,
        range_value: str | None,
        start: str | None,
        end: str | None,
        message: Mapping[str, Any],
    ) -> bool:
        if not name:
            return False
        if not isinstance(message, Mapping):
            return False

        payload = self._clone_payload(message)
        if not payload:
            return False

        if payload.get("type") != "strategy_performance":
            return False

        payload.setdefault("name", name)
        if range_value is not None and "range" not in payload:
            payload["range"] = self._coerce_text(range_value)
        if start is not None and "start" not in payload:
            payload["start"] = self._coerce_text(start)
        if end is not None and "end" not in payload:
            payload["end"] = self._coerce_text(end)

        if "timestamp" in payload:
            payload["timestamp"] = self._normalise_timestamp(payload["timestamp"])

        key = self._build_key(name, range_value, start, end)
        signature = self._compute_signature(payload)
        async with self._lock:
            self._prune_expired()
            existing = self._entries.get(key)
            if existing and existing.signature == signature:
                existing.stored_at = time.monotonic()
                return False
            self._entries[key] = _PerformanceEntry(
                payload=payload,
                stored_at=time.monotonic(),
                signature=signature,
            )
        return True

    async def get_message(
        self,
        name: str,
        range_value: str | None,
        start: str | None,
        end: str | None,
    ) -> Optional[Dict[str, Any]]:
        key = self._build_key(name, range_value, start, end)
        async with self._lock:
            self._prune_expired()
            entry = self._entries.get(key)
            if not entry:
                return None
            return self._clone_payload(entry.payload)

    async def remember_from_status(
        self,
        name: str,
        status: Mapping[str, Any],
        *,
        message_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(status, Mapping):
            return None

        performance = status.get("performance")
        if not isinstance(performance, Mapping):
            return None

        metrics: Any = (
            performance.get("metrics")
            or performance.get("data")
            or performance.get("payload")
        )
        if metrics is None:
            # Fallback to using the raw mapping minus metadata fields.
            metrics = {
                key: value
                for key, value in performance.items()
                if key
                not in {
                    "range",
                    "start",
                    "start_date",
                    "end",
                    "end_date",
                    "source",
                    "timestamp",
                }
            }
            if not metrics:
                return None

        range_value = performance.get("range") or status.get("range")
        start_value = (
            performance.get("start")
            or performance.get("start_date")
            or status.get("start")
        )
        end_value = (
            performance.get("end")
            or performance.get("end_date")
            or status.get("end")
        )

        if isinstance(metrics, Mapping):
            metrics_payload: Any = self._clone_payload(metrics)
        else:
            metrics_payload = self._clone_value(metrics)

        message: Dict[str, Any] = {
            "type": "strategy_performance",
            "success": True,
            "name": name,
            "range": range_value or "",
            "metrics": metrics_payload,
            "source": performance.get("source") or "stream",
        }

        stats = status.get("stats") or performance.get("stats")
        if isinstance(stats, Mapping):
            message["stats"] = self._clone_payload(stats)

        timestamp = (
            performance.get("timestamp")
            or status.get("timestamp")
            or status.get("updated_at")
            or status.get("created_at")
        )
        if timestamp is not None:
            message["timestamp"] = self._normalise_timestamp(timestamp)
        if message_id is not None:
            message["event_id"] = message_id

        stored = await self.store_message(
            name, range_value, start_value, end_value, message
        )
        if not stored:
            return None

        return self._clone_payload(message)


def _decode_json_mapping(data: Any) -> Dict[str, Any]:
    """Return a mapping parsed from a JSON payload if possible."""

    if not data:
        return {}

    if isinstance(data, Mapping):
        return {str(key): value for key, value in data.items()}

    text: str
    if isinstance(data, (bytes, bytearray)):
        try:
            text = data.decode("utf-8")
        except Exception:
            text = data.decode("utf-8", errors="ignore")
    elif isinstance(data, str):
        text = data
    else:
        return {}

    text = text.strip()
    if not text:
        return {}

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}

    if isinstance(payload, Mapping):
        return {str(key): value for key, value in payload.items()}
    return {}


class StrategyStatusStreamer:
    """Bridge strategy event stream updates to websocket clients."""

    def __init__(self) -> None:
        self._stream_key = os.getenv("STRATEGY_EVENT_STREAM_KEY", "stream:strategy:events").strip()
        self._metadata_prefix = os.getenv(
            "STRATEGY_METADATA_REDIS_PREFIX", "json:strategy"
        ).strip(" :")
        self._task: Optional[asyncio.Task[None]] = None
        self._lock = asyncio.Lock()
        self._running = False
        self._last_id: Optional[str] = None
        self._status_signatures: Dict[str, str] = {}
        self._statuses: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _clone_status(status: Mapping[str, Any]) -> Dict[str, Any]:
        if isinstance(status, Mapping):
            try:
                return deepcopy(status)  # type: ignore[arg-type]
            except Exception:
                try:
                    return dict(status)
                except Exception:
                    return {}
        return {}

    @staticmethod
    def _compute_signature(status: Mapping[str, Any]) -> str:
        try:
            return json.dumps(status, sort_keys=True, ensure_ascii=False)
        except TypeError:
            try:
                serialisable = json.loads(json.dumps(status, default=str))
            except Exception:
                serialisable = {str(key): str(value) for key, value in status.items()}
            if isinstance(serialisable, Mapping):
                serialisable = dict(serialisable)
            return json.dumps(serialisable, sort_keys=True, ensure_ascii=False)

    def _remember_status(self, strategy: str, status: Mapping[str, Any]) -> Dict[str, Any]:
        clone = self._clone_status(status)
        clone.setdefault("strategy", strategy)
        self._statuses[strategy] = clone
        return clone

    def remember_external_status(self, strategy: str, status: Mapping[str, Any]) -> None:
        stored = self._remember_status(strategy, status)
        self._status_signatures[strategy] = self._compute_signature(stored)

    async def start(self) -> None:
        """Ensure the background stream consumer is running."""

        if not self._stream_key:
            return

        async with self._lock:
            if self._task and not self._task.done():
                return
            self._running = True
            self._task = asyncio.create_task(
                self._run(), name="strategy-status-streamer"
            )

    async def _run(self) -> None:
        backoff = 1.0
        while self._running:
            try:
                redis = await get_redis_client()
            except Exception as exc:  # pragma: no cover - redis initialisation guard
                logger.debug(
                    "Failed to initialise Redis client for strategy stream", error=str(exc)
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue

            backoff = 1.0

            try:
                await self._consume(redis)
            except asyncio.CancelledError:  # pragma: no cover - cooperative shutdown
                raise
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.debug(
                    "Strategy status streamer encountered an error", error=str(exc)
                )
                await asyncio.sleep(1.0)

    async def _consume(self, redis) -> None:
        last_id = self._last_id or "$"
        while self._running:
            try:
                records = await redis.xread({self._stream_key: last_id}, block=1000, count=20)
            except asyncio.CancelledError:  # pragma: no cover - cooperative shutdown
                raise
            except Exception as exc:  # pragma: no cover - redis read guard
                logger.debug(
                    "Failed to read strategy event stream", stream=self._stream_key, error=str(exc)
                )
                await asyncio.sleep(1.0)
                break

            if not records:
                continue

            for _, entries in records:
                for message_id, payload in entries:
                    last_id = message_id
                    await self._handle_entry(redis, message_id, payload)

        self._last_id = last_id

    async def _handle_entry(self, redis, message_id: str, payload: Mapping[str, Any]) -> None:
        data = self._normalise_stream_payload(payload)
        strategy = (data.get("strategy") or data.get("name") or "").strip()
        if not strategy:
            return

        status = await self._load_status(redis, strategy)
        if not status:
            await asyncio.sleep(0.05)
            status = await self._load_status(redis, strategy)
            if not status:
                return

        previous_signature = self._status_signatures.get(strategy)
        stored_status = self._remember_status(strategy, status)
        new_signature = self._compute_signature(stored_status)
        self._status_signatures[strategy] = new_signature
        if previous_signature == new_signature:
            return

        message: Dict[str, Any] = {
            "type": "strategy_status",
            "success": True,
            "name": strategy,
            "status": self._clone_status(stored_status),
            "source": "stream",
            "event_id": message_id,
        }

        timestamp = (
            data.get("timestamp")
            or data.get("updated_at")
            or data.get("created_at")
            or data.get("ts")
        )
        if timestamp:
            message["timestamp"] = timestamp

        performance_message: Optional[Dict[str, Any]] = None
        try:
            performance_message = await _STRATEGY_PERFORMANCE_CACHE.remember_from_status(
                strategy,
                stored_status,
                message_id=message_id,
            )
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.debug(
                "Failed to cache streamed strategy performance",
                strategy=strategy,
                error=str(exc),
            )

        await _websocket_manager().broadcast(message)
        if performance_message:
            await _websocket_manager().broadcast(performance_message)

    async def get_cached_status(self, strategy: str) -> Optional[Tuple[Dict[str, Any], str]]:
        name = (strategy or "").strip()
        if not name:
            return None

        cached = self._statuses.get(name)
        if cached:
            if name not in self._status_signatures:
                self._status_signatures[name] = self._compute_signature(cached)
            return self._clone_status(cached), "stream"

        try:
            redis = await get_redis_client()
        except Exception as exc:  # pragma: no cover - redis initialisation guard
            logger.debug(
                "Failed to initialise Redis client for cached strategy status",
                strategy=name,
                error=str(exc),
            )
            return None

        status = await self._load_status(redis, name)
        if not status:
            return None

        stored = self._remember_status(name, status)
        self._status_signatures[name] = self._compute_signature(stored)
        return self._clone_status(stored), "cache"

    def _normalise_stream_payload(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in payload.items():
            if isinstance(key, (bytes, bytearray)):
                key = key.decode("utf-8", errors="ignore")
            key_text = str(key)
            if isinstance(value, (bytes, bytearray)):
                value = value.decode("utf-8", errors="ignore")
            result[key_text] = value

        signals = result.get("signals")
        if isinstance(signals, str):
            candidate = signals.strip()
            if candidate:
                try:
                    result["signals"] = json.loads(candidate)
                except json.JSONDecodeError:
                    pass

        orders = result.get("orders")
        if orders is not None and not isinstance(orders, (dict, list)):
            try:
                result["orders"] = int(float(str(orders)))
            except (TypeError, ValueError):
                result["orders"] = orders

        return result

    async def _load_status(self, redis, strategy: str) -> Dict[str, Any]:
        key = f"{self._metadata_prefix}:{strategy}:status"
        try:
            raw = await redis.get(key)
        except Exception as exc:  # pragma: no cover - redis read guard
            logger.debug(
                "Failed to read strategy status cache", strategy=strategy, error=str(exc)
            )
            return {}

        status = _decode_json_mapping(raw)
        if not status:
            return {}

        status.setdefault("strategy", strategy)
        return status


class RealtimeSubscriptionError(RuntimeError):
    """Raised when realtime price subscriptions via the data layer fail."""


@dataclass
class _RealtimeSymbolState:
    queues: set[asyncio.Queue[Dict[str, Any]]]
    last_message: Optional[Dict[str, Any]] = None


class RealtimeSubscription:
    """Handle tying a websocket connection to a realtime price stream."""

    __slots__ = ("symbol", "token", "queue", "_hub", "_closed")

    def __init__(
        self,
        hub: "RealtimePriceHub",
        symbol: str,
        token: DataSubscriptionToken,
        queue: "asyncio.Queue[Dict[str, Any]]",
    ) -> None:
        self.symbol = symbol
        self.token = token
        self.queue = queue
        self._hub = hub
        self._closed = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._hub._unsubscribe(self.symbol, self.queue, self.token)


class MarketDataPubSubError(RuntimeError):
    """Raised when realtime price subscriptions via Redis pubsub fail."""


class _RealtimeSubscriptionProtocol(Protocol):
    queue: "asyncio.Queue[Dict[str, Any]]"

    async def close(self) -> None:
        ...


def _build_realtime_message(
    symbol: str, payload: Mapping[str, Any], ts_ns: Optional[int] = None
) -> Dict[str, Any]:
    data = dict(payload)
    data.setdefault("symbol", symbol)
    ts_value = data.get("ts_ns")
    if isinstance(ts_value, int):
        ts_ns_value = ts_value
    else:
        if isinstance(ts_ns, int):
            ts_ns_value = ts_ns
        else:
            timestamp = data.get("timestamp")
            if isinstance(timestamp, (int, float)):
                ts_ns_value = int(float(timestamp) * 1_000_000)
            else:
                ts_ns_value = time.time_ns()
    data["ts_ns"] = ts_ns_value
    timestamp_ms = ts_ns_value // 1_000_000
    message = {
        "type": "realtime_data",
        "symbol": symbol,
        "timestamp": timestamp_ms,
        "timestamp_ms": timestamp_ms,
        "timestamp_ns": ts_ns_value,
        "data": data,
    }
    return message


class RealtimePriceHub:
    """Multiplex unified market tick updates across websocket clients."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._states: Dict[str, _RealtimeSymbolState] = {}
        self._manager = None
        self._event_token = None

    def _is_enabled(self) -> bool:
        return not is_legacy_mode(get_data_layer_mode())

    def _ensure_manager(self):
        if self._manager is not None:
            return self._manager
        try:
            manager = get_data_source_manager()
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.error("Failed to initialise data source manager: {}", exc)
            raise RealtimeSubscriptionError("Unified data layer unavailable") from exc
        self._manager = manager
        return manager

    def _ensure_event_subscription(self, manager) -> None:
        if self._event_token is not None:
            return
        self._event_token = manager.event_bus.subscribe(
            "market.tick", self._handle_tick_event
        )

    async def subscribe(self, symbol: str) -> RealtimeSubscription:
        symbol_key = (symbol or "").strip().upper()
        if not symbol_key:
            raise RealtimeSubscriptionError("Symbol is required")
        if not self._is_enabled():
            raise RealtimeSubscriptionError("Unified data layer disabled")

        logger.debug("RealtimePriceHub subscribe requested", symbol=symbol_key)
        manager = self._ensure_manager()
        self._ensure_event_subscription(manager)

        request = DataSubscriptionRequest(channel="market.tick", symbol=symbol_key)
        try:
            token = await manager.subscribe(request)
        except DataSourceError as exc:
            raise RealtimeSubscriptionError(str(exc) or "Failed to subscribe to market ticks") from exc
        except Exception as exc:  # pragma: no cover - defensive guard
            raise RealtimeSubscriptionError("Failed to subscribe to market ticks") from exc

        queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue(maxsize=5)

        snapshot_required = False
        last_message: Optional[Dict[str, Any]] = None

        async with self._lock:
            state = self._states.get(symbol_key)
            if state is None:
                state = _RealtimeSymbolState(queues=set())
                self._states[symbol_key] = state
                snapshot_required = True
            state.queues.add(queue)
            last_message = state.last_message

        initial_message: Optional[Dict[str, Any]] = None
        if last_message is not None:
            initial_message = last_message
        elif snapshot_required:
            initial_message = await self._load_snapshot(manager, request)
            if initial_message is not None:
                async with self._lock:
                    state = self._states.get(symbol_key)
                    if state and state.last_message is None:
                        state.last_message = initial_message

        if initial_message is not None:
            self._offer(queue, initial_message)

        async with self._lock:
            state = self._states.get(symbol_key)
            consumer_count = len(state.queues) if state else 0
        logger.debug(
            "RealtimePriceHub subscription established",
            symbol=symbol_key,
            snapshot_delivered=bool(initial_message),
            consumers=consumer_count,
        )
        return RealtimeSubscription(self, symbol_key, token, queue)

    async def _load_snapshot(
        self,
        manager,
        request: DataSubscriptionRequest,
    ) -> Optional[Dict[str, Any]]:
        try:
            snapshot = await manager.snapshot(request)
        except DataSourceError as exc:
            logger.debug(
                "Failed to load market snapshot during subscription", symbol=request.symbol, error=str(exc)
            )
            return None
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.debug(
                "Unexpected error loading market snapshot", symbol=request.symbol, error=str(exc)
            )
            return None

        if isinstance(snapshot, Mapping) and snapshot:
            return self._build_message(request.symbol or "", snapshot)
        return None

    async def _unsubscribe(
        self,
        symbol: str,
        queue: "asyncio.Queue[Dict[str, Any]]",
        token: DataSubscriptionToken,
    ) -> None:
        async with self._lock:
            state = self._states.get(symbol)
            if state:
                state.queues.discard(queue)
                if not state.queues:
                    self._states.pop(symbol, None)
        logger.debug("RealtimePriceHub unsubscribing", symbol=symbol)

        manager = self._manager
        if manager is not None:
            try:
                await manager.unsubscribe(token)
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.debug(
                    "Failed to release market tick subscription", symbol=symbol, error=str(exc)
                )

    async def _handle_tick_event(self, envelope: EventEnvelope) -> None:
        symbol = (envelope.symbol or "").strip().upper()
        if not symbol:
            return

        message = _build_realtime_message(symbol, envelope.payload or {}, envelope.ts_ns)

        async with self._lock:
            state = self._states.get(symbol)
            if not state or not state.queues:
                return
            state.last_message = message
            queues = list(state.queues)

        for queue in queues:
            self._offer(queue, message)

    def _build_message(
        self,
        symbol: str,
        payload: Mapping[str, Any],
        ts_ns: Optional[int] = None,
    ) -> Dict[str, Any]:
        return _build_realtime_message(symbol, payload, ts_ns)

    @staticmethod
    def _offer(queue: "asyncio.Queue[Dict[str, Any]]", message: Dict[str, Any]) -> None:
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:  # pragma: no cover - best effort
                pass


class MarketDataPubSubSubscription:
    __slots__ = ("symbol", "queue", "_hub", "_closed")

    def __init__(
        self,
        hub: "MarketDataPubSubHub",
        symbol: str,
        queue: "asyncio.Queue[Dict[str, Any]]",
    ) -> None:
        self.symbol = symbol
        self.queue = queue
        self._hub = hub
        self._closed = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._hub._unsubscribe(self.symbol, self.queue)


class MarketDataPubSubHub:
    """Stream market data updates via Redis pubsub when the data layer is unavailable."""

    PRICE_CHANNEL_TEMPLATE = "pubsub:ib:price:{symbol}"

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._states: Dict[str, _RealtimeSymbolState] = {}
        self._listener = PubSubListener(redis_factory=get_redis_client)
        self._listener_lock = asyncio.Lock()
        self._listener_started = False

    async def _ensure_listener(self) -> None:
        if self._listener_started:
            return
        async with self._listener_lock:
            if self._listener_started:
                return
            await self._listener.start()
            self._listener_started = True

    def _channel_name(self, symbol: str) -> str:
        return self.PRICE_CHANNEL_TEMPLATE.format(symbol=symbol)

    async def subscribe(self, symbol: str) -> MarketDataPubSubSubscription:
        symbol_key = (symbol or "").strip().upper()
        if not symbol_key:
            raise MarketDataPubSubError("Symbol is required")

        try:
            await self._ensure_listener()
        except Exception as exc:
            raise MarketDataPubSubError("Failed to start market data pubsub listener") from exc

        queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue(maxsize=5)

        snapshot_required = False
        last_message: Optional[Dict[str, Any]] = None

        async with self._lock:
            state = self._states.get(symbol_key)
            if state is None:
                state = _RealtimeSymbolState(queues=set())
                self._states[symbol_key] = state
                snapshot_required = True
                channel = self._channel_name(symbol_key)
                self._listener.register_callback(channel, self._handle_price_message)
            state.queues.add(queue)
            last_message = state.last_message

        if last_message is None and snapshot_required:
            snapshot = await _fetch_market_snapshot(symbol_key)
            if isinstance(snapshot, Mapping) and snapshot:
                last_message = _build_realtime_message(symbol_key, snapshot)
                async with self._lock:
                    state = self._states.get(symbol_key)
                    if state is not None:
                        state.last_message = last_message

        if last_message is not None:
            RealtimePriceHub._offer(queue, last_message)

        async with self._lock:
            state = self._states.get(symbol_key)
            consumer_count = len(state.queues) if state else 0
        logger.debug(
            "MarketDataPubSubHub subscription established",
            symbol=symbol_key,
            snapshot_delivered=bool(last_message),
            consumers=consumer_count,
        )
        return MarketDataPubSubSubscription(self, symbol_key, queue)

    async def _unsubscribe(
        self,
        symbol: str,
        queue: "asyncio.Queue[Dict[str, Any]]",
    ) -> None:
        channel = None
        async with self._lock:
            state = self._states.get(symbol)
            if state:
                state.queues.discard(queue)
                if not state.queues:
                    self._states.pop(symbol, None)
                    channel = self._channel_name(symbol)
        logger.debug("MarketDataPubSubHub unsubscribing", symbol=symbol)
        if channel is not None:
            self._listener.unregister_callback(channel, self._handle_price_message)

    async def _handle_price_message(self, channel: str, payload: str) -> None:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            logger.debug("Invalid market data payload on {}", channel)
            return
        symbol = str(data.get("symbol") or "").strip().upper()
        if not symbol:
            logger.debug("Market data payload missing symbol on {}", channel)
            return

        message = _build_realtime_message(symbol, data)

        async with self._lock:
            state = self._states.get(symbol)
            if not state or not state.queues:
                return
            state.last_message = message
            queues = list(state.queues)

        for queue in queues:
            RealtimePriceHub._offer(queue, message)


_REALTIME_PRICE_HUB = RealtimePriceHub()
_MARKET_DATA_PUBSUB_HUB = MarketDataPubSubHub()


_STRATEGY_PERFORMANCE_CACHE = StrategyPerformanceCache()


_STRATEGY_STATUS_STREAMER = StrategyStatusStreamer()


async def _realtime_price_sender(
    ws: WebSocket,
    symbol: str,
    interval: float = 2.0,
):
    try:
        while True:
            health = await _load_ib_command_health()
            if not health or health.get("stale"):
                await asyncio.sleep(interval)
                continue
            if not bool(health.get("connected")):
                await asyncio.sleep(interval)
                continue
            try:
                data = await _fetch_market_snapshot(symbol)
                if data is not None:
                    await ws.send_text(
                        json.dumps(
                            {
                                "type": "realtime_data",
                                "symbol": symbol,
                                "data": data,
                            }
                        )
                    )
                else:
                    logger.debug(f"Skipping null realtime data for {symbol}")
            except Exception as e:
                logger.debug(f"Realtime data error for {symbol}: {e}")
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        pass

@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await _ensure_risk_rules_listener()
    await _STRATEGY_STATUS_STREAMER.start()
    await _websocket_manager().connect(websocket)

    realtime_streams: Dict[str, Tuple[asyncio.Task, Optional[_RealtimeSubscriptionProtocol]]] = {}

    account_service_outage_logged = False
    account_service_readiness: Optional[bool] = None

    def _coerce_readiness(value: Any) -> Optional[bool]:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if not text:
            return None
        if text in {"1", "true", "yes", "ready", "online", "healthy"}:
            return True
        if text in {"0", "false", "no", "offline", "unready", "down"}:
            return False
        return None

    def _update_account_service_readiness(payload: Optional[Mapping[str, Any]]) -> None:
        nonlocal account_service_readiness

        readiness_value: Optional[bool] = None
        if isinstance(payload, Mapping):
            for key in (
                "account_service_ready",
                "account_service_online",
                "account_service_available",
            ):
                if key not in payload:
                    continue
                readiness_value = _coerce_readiness(payload.get(key))
                if readiness_value is not None:
                    break

        account_service_readiness = readiness_value

    def _log_account_service_outage(context: str) -> None:
        nonlocal account_service_outage_logged

        if account_service_outage_logged:
            return

        logger.warning(
            "Account service unavailable during {} (account_service_ready={})",
            context,
            account_service_readiness,
        )
        account_service_outage_logged = True

    async def _resolve_connection_context(
        payload: Optional[Mapping[str, Any]] = None,
        *,
        refresh: bool = False,
    ) -> ConnectionContext:
        if refresh or payload is None:
            payload = await _load_ib_command_health()

        data: Dict[str, Any] = {}
        if isinstance(payload, Mapping):
            data = dict(payload)
        elif payload:
            try:
                data = dict(payload)  # type: ignore[arg-type]
            except Exception:
                data = {}

        redis_fresh = bool(data) and not bool(data.get("stale", True))
        redis_connected = bool(data.get("connected")) if redis_fresh else None

        connected_flag = bool(redis_connected)
        account_type_raw = data.get("account_type") if data else None
        account_type = (
            str(account_type_raw)
            if account_type_raw
            else resolve_ib_account_type()
        )
        last_error = data.get("last_error") or None
        error_code_value = data.get("error_code") if data else None
        if isinstance(error_code_value, str) and not error_code_value.strip():
            error_code_value = None

        error_message = None
        if not data:
            error_message = f"{SERVICE_UNAVAILABLE_MESSAGE}：未获取到 ib-command 健康数据"
            last_error = last_error or error_message

        return ConnectionContext(
            data if data else None,
            bool(connected_flag),
            account_type,
            last_error,
            redis_connected,
            redis_fresh,
            error_message,
            error_code_value,
        )

    health_payload: Optional[Dict[str, Any]]
    context = await _resolve_connection_context(refresh=True)
    health_payload = context.health_payload
    connected_flag = context.connected
    account_type_label = context.account_type
    last_error_value = context.last_error
    health_error = context.health_error
    error_code_value = context.error_code

    _update_account_service_readiness(health_payload)

    # 新连接建立后，立即向该客户端单播当前连接状态
    payload = {
        "type": "connection_status",
        "success": health_payload is not None,
        "connected": bool(connected_flag),
        "account_type": account_type_label,
        "error_code": error_code_value,
    }
    if last_error_value:
        payload["last_error"] = last_error_value
    if health_payload is not None:
        payload["health"] = health_payload
        if health_payload.get("stale"):
            payload["stale"] = True
    if health_error:
        payload["error"] = health_error
    if not await _send_json(websocket, payload):
        _websocket_manager().disconnect(websocket)
        return

    # 初次连接后，向该连接单播账户信息快照
    account_payloads: List[Dict[str, Any]] = []
    try:
        snapshot = await read_account_info_snapshot()
        snapshot_metadata = (
            _merge_metadata(snapshot) if isinstance(snapshot, Mapping) else {}
        )
        stale_snapshot = AccountServiceClient.is_metadata_stale(snapshot_metadata)
        info_result = await _fetch_account_info(force_refresh=stale_snapshot)
        unavailable_payload = (
            _extract_service_unavailable_payload(info_result, "account_update")
            if isinstance(info_result, Mapping)
            else None
        )

        if isinstance(snapshot, Mapping):
            if "source" not in snapshot_metadata or not snapshot_metadata.get("source"):
                snapshot_metadata["source"] = (
                    snapshot.get("source") or IB_COMMAND_SOURCE
                )
        snapshot_data = snapshot.get("data") if isinstance(snapshot, Mapping) else None

        if unavailable_payload is not None:
            payload = dict(unavailable_payload)
            account_info_payload: Dict[str, Any] = {}
            if isinstance(payload.get("account_info"), Mapping):
                account_info_payload = dict(payload["account_info"])
            if isinstance(snapshot_data, Mapping):
                account_info_payload.update(dict(snapshot_data))
            account_info_payload.setdefault("connected", bool(connected_flag))
            if account_type_label:
                account_info_payload.setdefault("account_type", account_type_label)
            if last_error_value and "last_error" not in account_info_payload:
                account_info_payload["last_error"] = last_error_value
            payload["account_info"] = account_info_payload

            combined_meta = _merge_metadata(payload, snapshot_metadata)
            if not payload.get("source"):
                payload["source"] = combined_meta.get("source") or IB_COMMAND_SOURCE
            if combined_meta.get("snapshot_updated_at") and "snapshot_updated_at" not in payload:
                payload["snapshot_updated_at"] = combined_meta["snapshot_updated_at"]
            if combined_meta.get("stale_reason") and "stale_reason" not in payload:
                payload["stale_reason"] = combined_meta["stale_reason"]
            if combined_meta.get("stale_at") and "stale_at" not in payload:
                payload["stale_at"] = combined_meta["stale_at"]
            if "from_cache" not in payload and "from_cache" in combined_meta:
                payload["from_cache"] = bool(combined_meta.get("from_cache"))
            payload.setdefault(
                "stale", AccountServiceClient.is_metadata_stale(combined_meta)
            )
            account_payloads.append(payload)
        else:
            combined_meta = dict(snapshot_metadata)
            info_payload: Any = None
            if isinstance(info_result, AccountServiceResult):
                info_payload = info_result.data
                for key, value in info_result.metadata.items():
                    if value is None:
                        continue
                    combined_meta[key] = value
            elif isinstance(info_result, Mapping):
                info_payload = dict(info_result)
                combined_meta.update(
                    {
                        key: value
                        for key, value in info_result.items()
                        if key in _METADATA_KEYS and value is not None
                    }
                )

            if info_payload is None and isinstance(snapshot_data, Mapping):
                info_payload = dict(snapshot_data)

            if info_payload is not None:
                if isinstance(info_payload, Mapping):
                    info_payload = dict(info_payload)
                    info_payload.setdefault("connected", bool(connected_flag))
                    if account_type_label:
                        info_payload.setdefault("account_type", account_type_label)
                    if last_error_value and "last_error" not in info_payload:
                        info_payload["last_error"] = last_error_value

                source_value = combined_meta.get("source")
                if not source_value:
                    if isinstance(info_result, AccountServiceResult):
                        source_value = (
                            info_result.metadata.get("source") or IB_COMMAND_SOURCE
                        )
                    else:
                        source_value = (
                            snapshot_metadata.get("source") or IB_COMMAND_SOURCE
                        )

                stale_flag = AccountServiceClient.is_metadata_stale(combined_meta)
                response_payload: Dict[str, Any] = {
                    "type": "account_update",
                    "success": True,
                    "account_info": info_payload,
                    "source": source_value or IB_COMMAND_SOURCE,
                    "stale": stale_flag,
                    "from_cache": bool(combined_meta.get("from_cache")),
                }
                if combined_meta.get("snapshot_updated_at"):
                    response_payload["snapshot_updated_at"] = combined_meta[
                        "snapshot_updated_at"
                    ]
                if combined_meta.get("stale_reason"):
                    response_payload["stale_reason"] = combined_meta["stale_reason"]
                if combined_meta.get("stale_at"):
                    response_payload["stale_at"] = combined_meta["stale_at"]
                if combined_meta:
                    response_payload["metadata"] = dict(combined_meta)
                for key in (
                    "fast",
                    "force_refresh",
                    "requested_fast",
                    "requested_force_refresh",
                ):
                    if key in combined_meta and key not in response_payload:
                        response_payload[key] = combined_meta[key]
                for key in (
                    "fast",
                    "force_refresh",
                    "requested_fast",
                    "requested_force_refresh",
                ):
                    if key in combined_meta and key not in response_payload:
                        response_payload[key] = combined_meta[key]

                account_payloads.append(response_payload)

                if stale_flag:
                    status_payload: Dict[str, Any] = {
                        "type": "account_snapshot_status",
                        "snapshot": "account_info",
                        "stale": True,
                        "refresh_in_progress": True,
                    }
                    if combined_meta.get("snapshot_updated_at"):
                        status_payload["snapshot_updated_at"] = combined_meta[
                            "snapshot_updated_at"
                        ]
                    if combined_meta.get("stale_reason"):
                        status_payload["stale_reason"] = combined_meta["stale_reason"]
                    if combined_meta.get("stale_at"):
                        status_payload["stale_at"] = combined_meta["stale_at"]
                    account_payloads.append(status_payload)
            else:
                combined_meta.setdefault(
                    "source", snapshot_metadata.get("source") or IB_COMMAND_SOURCE
                )
                fallback_payload = _service_unavailable_payload(
                    "account_update",
                    account_info={
                        "connected": bool(connected_flag),
                        "account_type": account_type_label,
                        "last_error": last_error_value,
                    },
                    stale=AccountServiceClient.is_metadata_stale(combined_meta),
                    snapshot_updated_at=combined_meta.get("snapshot_updated_at"),
                )
                if combined_meta.get("stale_reason"):
                    fallback_payload["stale_reason"] = combined_meta["stale_reason"]
                if combined_meta.get("stale_at"):
                    fallback_payload["stale_at"] = combined_meta["stale_at"]
                fallback_payload["source"] = (
                    combined_meta.get("source") or IB_COMMAND_SOURCE
                )
                if combined_meta:
                    fallback_payload["metadata"] = dict(combined_meta)
                for key in (
                    "fast",
                    "force_refresh",
                    "requested_fast",
                    "requested_force_refresh",
                ):
                    if key in combined_meta and key not in fallback_payload:
                        fallback_payload[key] = combined_meta[key]
                account_payloads.append(fallback_payload)
    except Exception as e:
        logger.debug(f"Failed to send initial account_update: {e}")

    for payload_to_send in account_payloads:
        if not await _send_json(websocket, payload_to_send):
            _websocket_manager().disconnect(websocket)
            return

    # 初次连接后，向该连接单播发送订单快照（最多300条）
    initial_limit = 10
    try:
        orders_result = await _fetch_orders(limit=initial_limit, offset=0)
        unavailable_orders = _extract_service_unavailable_payload(
            orders_result, "orders_snapshot"
        )
        if unavailable_orders is not None:
            _log_account_service_outage("initial orders snapshot")
            unavailable_orders.setdefault("meta", {"limit": initial_limit, "page": 1})
            payload = unavailable_orders
        elif orders_result is None:
            _log_account_service_outage("initial orders snapshot")
            payload = _service_unavailable_payload(
                "orders_snapshot",
                meta={"limit": initial_limit, "page": 1},
            )
        else:
            orders, total, metadata = orders_result
            payload = _orders_payload(
                orders,
                limit=initial_limit,
                start_date=metadata.get("start_date"),
                start_date_timezone=metadata.get("start_date_timezone"),
                page=1,
                total=total,
            )
    except Exception as e:
        logger.warning(f"Failed to send initial orders snapshot: {e}")
        payload = _service_unavailable_payload(
            "orders_snapshot",
            meta={"limit": initial_limit, "page": 1},
        )

    if not await _send_json(websocket, payload):
        _websocket_manager().disconnect(websocket)
        return

    # 初次连接后，向该连接单播发送positions快照
    position_payloads: List[Dict[str, Any]] = []
    try:
        positions_snapshot = await read_positions_snapshot()
        positions_snapshot_metadata = (
            _merge_metadata(positions_snapshot)
            if isinstance(positions_snapshot, Mapping)
            else {}
        )
        stale_positions = AccountServiceClient.is_metadata_stale(
            positions_snapshot_metadata
        )
        requested_fast = True
        requested_force_refresh = stale_positions
        positions_result = await _fetch_positions(
            force_refresh=requested_force_refresh,
            fast=requested_fast,
        )
        unavailable_positions = (
            _extract_service_unavailable_payload(positions_result, "position_update")
            if isinstance(positions_result, Mapping)
            else None
        )

        if isinstance(positions_snapshot, Mapping):
            if not positions_snapshot_metadata.get("source"):
                positions_snapshot_metadata["source"] = (
                    positions_snapshot.get("source") or IB_COMMAND_SOURCE
                )
        positions_snapshot_data = (
            positions_snapshot.get("data") if isinstance(positions_snapshot, Mapping) else None
        )

        if unavailable_positions is not None:
            payload = dict(unavailable_positions)
            combined_meta = _merge_metadata(payload, positions_snapshot_metadata)
            if isinstance(positions_snapshot_data, list) and not payload.get("positions"):
                payload["positions"] = list(positions_snapshot_data)
            payload.setdefault("positions", [])
            if not payload.get("source"):
                payload["source"] = (
                    combined_meta.get("source") or IB_COMMAND_SOURCE
                )
            if combined_meta.get("snapshot_updated_at") and "snapshot_updated_at" not in payload:
                payload["snapshot_updated_at"] = combined_meta["snapshot_updated_at"]
            if combined_meta.get("stale_reason") and "stale_reason" not in payload:
                payload["stale_reason"] = combined_meta["stale_reason"]
            if combined_meta.get("stale_at") and "stale_at" not in payload:
                payload["stale_at"] = combined_meta["stale_at"]
            if "from_cache" not in payload and "from_cache" in combined_meta:
                payload["from_cache"] = bool(combined_meta.get("from_cache"))
            payload.setdefault(
                "stale", AccountServiceClient.is_metadata_stale(combined_meta)
            )
            payload.setdefault("fast", bool(requested_fast))
            payload.setdefault("force_refresh", bool(requested_force_refresh))
            position_payloads.append(payload)
        else:
            combined_meta = dict(positions_snapshot_metadata)
            positions_payload: Optional[List[Dict[str, Any]]] = None

            if isinstance(positions_result, AccountServiceResult):
                data = positions_result.data
                if isinstance(data, list):
                    positions_payload = [
                        dict(item) if isinstance(item, Mapping) else item for item in data
                    ]
                for key, value in positions_result.metadata.items():
                    if value is None:
                        continue
                    combined_meta[key] = value
            elif isinstance(positions_result, Mapping):
                raw_positions = positions_result.get("positions") or positions_result.get("data")
                if isinstance(raw_positions, list):
                    positions_payload = [
                        dict(item) if isinstance(item, Mapping) else item
                        for item in raw_positions
                    ]
                combined_meta.update(
                    {
                        key: value
                        for key, value in positions_result.items()
                        if key in _METADATA_KEYS and value is not None
                    }
                )

            if positions_payload is None and isinstance(positions_snapshot_data, list):
                positions_payload = [
                    dict(item) if isinstance(item, Mapping) else item
                    for item in positions_snapshot_data
                ]

            if positions_payload is not None:
                source_value = combined_meta.get("source")
                if not source_value:
                    if isinstance(positions_result, AccountServiceResult):
                        source_value = (
                            positions_result.metadata.get("source")
                            or IB_COMMAND_SOURCE
                        )
                    else:
                        source_value = (
                            positions_snapshot_metadata.get("source")
                            or IB_COMMAND_SOURCE
                        )

                stale_flag = AccountServiceClient.is_metadata_stale(combined_meta)
                response_payload = {
                    "type": "position_update",
                    "success": True,
                    "positions": positions_payload,
                    "source": source_value or IB_COMMAND_SOURCE,
                    "stale": stale_flag,
                    "from_cache": bool(combined_meta.get("from_cache")),
                }
                response_payload.setdefault("fast", bool(requested_fast))
                response_payload.setdefault(
                    "force_refresh", bool(requested_force_refresh)
                )
                if combined_meta.get("snapshot_updated_at"):
                    response_payload["snapshot_updated_at"] = combined_meta[
                        "snapshot_updated_at"
                    ]
                if combined_meta.get("stale_reason"):
                    response_payload["stale_reason"] = combined_meta["stale_reason"]
                if combined_meta.get("stale_at"):
                    response_payload["stale_at"] = combined_meta["stale_at"]
                if combined_meta:
                    response_payload["metadata"] = dict(combined_meta)

                position_payloads.append(response_payload)

                if stale_flag:
                    status_payload = {
                        "type": "account_snapshot_status",
                        "snapshot": "positions",
                        "stale": True,
                        "refresh_in_progress": True,
                    }
                    if combined_meta.get("snapshot_updated_at"):
                        status_payload["snapshot_updated_at"] = combined_meta[
                            "snapshot_updated_at"
                        ]
                    if combined_meta.get("stale_reason"):
                        status_payload["stale_reason"] = combined_meta["stale_reason"]
                    if combined_meta.get("stale_at"):
                        status_payload["stale_at"] = combined_meta["stale_at"]
                    position_payloads.append(status_payload)
            else:
                combined_meta.setdefault(
                    "source",
                    positions_snapshot_metadata.get("source") or IB_COMMAND_SOURCE,
                )
                fallback_payload = _service_unavailable_payload(
                    "position_update",
                    positions=[],
                    source=combined_meta.get("source"),
                    stale=AccountServiceClient.is_metadata_stale(combined_meta),
                    snapshot_updated_at=combined_meta.get("snapshot_updated_at"),
                )
                if combined_meta.get("stale_reason"):
                    fallback_payload["stale_reason"] = combined_meta["stale_reason"]
                if combined_meta.get("stale_at"):
                    fallback_payload["stale_at"] = combined_meta["stale_at"]
                if combined_meta:
                    fallback_payload["metadata"] = dict(combined_meta)
                for key in (
                    "fast",
                    "force_refresh",
                    "requested_fast",
                    "requested_force_refresh",
                ):
                    if key in combined_meta and key not in fallback_payload:
                        fallback_payload[key] = combined_meta[key]
                position_payloads.append(fallback_payload)
    except Exception as e:
        logger.debug(f"Failed to send initial positions snapshot: {e}")

    for payload_to_send in position_payloads:
        if not await _send_json(websocket, payload_to_send):
            _websocket_manager().disconnect(websocket)
            return

    request_tracker: Dict[str, float] = {}
    last_responses: Dict[str, Dict[str, Any]] = {}
    dom_client = get_dom_service_client()
    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            msg_type = message.get("type")
            if msg_type == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
            elif msg_type == "request_connection_status":
                key = "connection_status"
                force = bool(message.get("force"))
                if not _allow_request(request_tracker, key, force=force):
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        cached_payload["cached"] = True
                        await websocket.send_text(json.dumps(cached_payload))
                    else:
                        await websocket.send_text(
                            json.dumps(
                                {
                                    "type": "connection_status",
                                    "success": False,
                                    "error": "Rate limited",
                                }
                            )
                        )
                    continue

                context = await _resolve_connection_context(health_payload, refresh=True)
                health_payload = context.health_payload
                connected_flag = context.connected
                account_type_label = context.account_type
                last_error_value = context.last_error
                redis_connected = context.redis_connected
                redis_fresh = context.redis_fresh
                health_error = context.health_error
                error_code_value = context.error_code

                _update_account_service_readiness(health_payload)

                payload = {
                    "type": "connection_status",
                    "success": health_payload is not None,
                    "connected": bool(connected_flag),
                    "account_type": account_type_label,
                    "source": "websocket",
                }

                if error_code_value:
                    payload["error_code"] = error_code_value
                if last_error_value:
                    payload["last_error"] = last_error_value
                if health_payload is not None:
                    payload["health"] = health_payload
                    if health_payload.get("stale"):
                        payload["stale"] = True
                if health_error:
                    payload["error"] = health_error
                if redis_connected is None:
                    payload["from_cache"] = False
                payload["fresh"] = bool(redis_fresh)

                last_responses[key] = payload
                await websocket.send_text(json.dumps(payload))
            elif msg_type == "request_service_statuses":
                key = "service_statuses"
                force = bool(message.get("force"))
                if not _allow_request(request_tracker, key, force=force):
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        cached_payload["cached"] = True
                        await _send_or_disconnect(websocket, cached_payload)
                    else:
                        await _send_or_disconnect(
                            websocket,
                            {
                                "type": "service_statuses",
                                "success": False,
                                "error": "Rate limited",
                                "source": "websocket",
                            },
                        )
                    continue

                try:
                    container = core.get_container()
                except Exception:
                    container = None

                redis_client = None
                if container is not None:
                    redis_client = getattr(container, "redis_client", None)

                try:
                    raw_payload = await build_service_status_payload(redis=redis_client)
                    payload = {
                        "type": "service_statuses",
                        "success": True,
                        "source": "websocket",
                    }
                    if isinstance(raw_payload, Mapping):
                        payload.update(dict(raw_payload))
                    else:
                        payload["data"] = raw_payload
                except Exception as exc:
                    logger.warning(f"Failed to build service status payload: {exc}")
                    payload = {
                        "type": "service_statuses",
                        "success": False,
                        "error": str(exc),
                        "source": "websocket",
                    }

                last_responses[key] = payload
                await _send_or_disconnect(websocket, payload)
            elif msg_type == "subscribe_realtime":
                raw_symbol = message.get("symbol")
                symbol_key = (raw_symbol or "").strip().upper()
                if not symbol_key or symbol_key in realtime_streams:
                    continue

                try:
                    subscription = await _REALTIME_PRICE_HUB.subscribe(symbol_key)
                except RealtimeSubscriptionError as exc:
                    logger.debug(
                        "Falling back to Redis market data feed", symbol=symbol_key, error=str(exc)
                    )
                    try:
                        subscription = await _MARKET_DATA_PUBSUB_HUB.subscribe(symbol_key)
                    except MarketDataPubSubError as pubsub_exc:
                        logger.debug(
                            "Falling back to polling realtime feed",
                            symbol=symbol_key,
                            error=str(pubsub_exc),
                        )
                        task = asyncio.create_task(
                            _realtime_price_sender(websocket, symbol_key)
                        )
                        realtime_streams[symbol_key] = (task, None)
                    else:
                        forward_task = asyncio.create_task(
                            _forward_realtime_subscription(websocket, subscription)
                        )
                        realtime_streams[symbol_key] = (forward_task, subscription)
                else:
                    forward_task = asyncio.create_task(
                        _forward_realtime_subscription(websocket, subscription)
                    )
                    realtime_streams[symbol_key] = (forward_task, subscription)
            elif msg_type == "unsubscribe_realtime":
                raw_symbol = message.get("symbol")
                symbol_key = (raw_symbol or "").strip().upper()
                entry = realtime_streams.pop(symbol_key, None)
                if entry:
                    task, subscription = entry
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                    if subscription is not None:
                        await subscription.close()
            elif msg_type == "request_orders_snapshot":
                try:
                    limit = int(message.get("limit", 10))
                except (TypeError, ValueError):
                    limit = 10
                try:
                    page = int(message.get("page", 1))
                except (TypeError, ValueError):
                    page = 1
                if limit <= 0:
                    limit = 10
                if page < 1:
                    page = 1
                offset = (page - 1) * limit

                start_date_raw = message.get("start_date")
                start_dt = parse_local_datetime(start_date_raw)
                start_date_iso = start_dt.isoformat() if start_dt else None
                if isinstance(start_date_raw, str):
                    raw_trimmed = start_date_raw.strip()
                else:
                    raw_trimmed = ""
                start_date_meta_value = start_date_iso or (raw_trimmed or None)
                if start_dt is not None and start_dt.tzinfo is not None:
                    start_date_tz_meta_value = getattr(start_dt.tzinfo, "key", None) or start_dt.tzinfo.tzname(start_dt)
                else:
                    start_date_tz_meta_value = None
                cache_key_date = start_date_meta_value or ""
                key = f"orders_snapshot:{limit}:{cache_key_date}:{page}"
                force = bool(message.get("force"))
                if not _allow_request(request_tracker, key, force=force):
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        cached_payload["cached"] = True
                        await websocket.send_text(json.dumps(cached_payload))
                    else:
                        await websocket.send_text(json.dumps({
                            "type": "orders_snapshot",
                            "success": False,
                            "error": "Rate limited",
                        }))
                    continue

                try:
                    result = await _fetch_orders(
                        limit=limit,
                        offset=offset,
                        start_date=start_date_iso,
                    )
                    unavailable_orders = _extract_service_unavailable_payload(
                        result, "orders_snapshot"
                    )
                    if unavailable_orders is not None:
                        _log_account_service_outage("orders snapshot request")
                        unavailable_orders.setdefault(
                            "meta",
                            {
                                "limit": limit,
                                "page": page,
                                "start_date": start_date_meta_value,
                                "start_date_timezone": start_date_tz_meta_value,
                            },
                        )
                        payload = unavailable_orders
                    elif result is None:
                        _log_account_service_outage("orders snapshot request")
                        payload = _service_unavailable_payload(
                            "orders_snapshot",
                            meta={
                                "limit": limit,
                                "page": page,
                                "start_date": start_date_meta_value,
                                "start_date_timezone": start_date_tz_meta_value,
                            },
                        )
                    else:
                        orders, total, metadata = result
                        effective_start_date = (
                            metadata.get("start_date")
                            if isinstance(metadata, Mapping)
                            else None
                        )
                        if effective_start_date is None:
                            effective_start_date = start_date_meta_value
                        effective_start_timezone = (
                            metadata.get("start_date_timezone")
                            if isinstance(metadata, Mapping)
                            else None
                        )
                        if effective_start_timezone is None:
                            effective_start_timezone = start_date_tz_meta_value
                        payload = _orders_payload(
                            orders,
                            limit=limit,
                            start_date=effective_start_date,
                            start_date_timezone=effective_start_timezone,
                            page=page,
                            total=total,
                        )
                    last_responses[key] = payload
                    await websocket.send_text(json.dumps(payload))
                except Exception as exc:
                    logger.warning(f"Failed to stream orders snapshot: {exc}")
                    error_payload = _service_unavailable_payload(
                        "orders_snapshot",
                        meta={
                            "limit": limit,
                            "page": page,
                            "start_date": start_date_raw,
                            "start_date_timezone": start_date_tz_meta_value,
                        },
                    )
                    error_payload["details"] = str(exc)
                    last_responses[key] = error_payload
                    await websocket.send_text(json.dumps(error_payload))
            elif msg_type == "request_logs_snapshot":
                range_value = (message.get("range") or "today").strip() or "today"
                module_value = (message.get("module") or "all").strip() or "all"
                level_value = (message.get("level") or "all").strip() or "all"
                search_value = message.get("search")
                raw_request_filter = message.get("filter_request_id") or message.get("request_id_filter")
                request_filter = None
                if isinstance(raw_request_filter, str):
                    request_filter = raw_request_filter.strip() or None
                request_token = message.get("request_id")

                try:
                    page_size = int(message.get("page_size", 200))
                except (TypeError, ValueError):
                    page_size = 200
                try:
                    cursor = message.get("cursor")
                    cursor_value = int(cursor) if cursor is not None else None
                except (TypeError, ValueError):
                    cursor_value = None

                direction_value = (message.get("direction") or "backward").strip() or "backward"
                direction_value = direction_value if direction_value in {"backward", "forward"} else "backward"

                try:
                    lines_value = int(message.get("lines", 50))
                except (TypeError, ValueError):
                    lines_value = 50

                key = (
                    "logs_snapshot:" +
                    ":".join(
                        str(part)
                        for part in (
                            range_value,
                            module_value,
                            level_value,
                            direction_value,
                            cursor_value if cursor_value is not None else "",
                            page_size,
                            request_filter or "",
                            search_value or "",
                            lines_value,
                        )
                    )
                )
                force = bool(message.get("force"))
                if not _allow_request(request_tracker, key, force=force):
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        cached_payload["cached"] = True
                        if request_token is not None:
                            cached_payload["request_id"] = request_token
                        await _send_or_disconnect(websocket, cached_payload)
                    else:
                        await _send_or_disconnect(
                            websocket,
                            {
                                "type": "logs_snapshot",
                                "success": False,
                                "error": "Rate limited",
                                "query": {
                                    "range": range_value,
                                    "module": module_value,
                                    "level": level_value,
                                "direction": direction_value,
                                "cursor": cursor_value,
                                "page_size": page_size,
                                "request_id": request_filter,
                                "search": search_value,
                                "lines": lines_value,
                            },
                        },
                    )
                    continue

                try:
                    result = await load_logs_data(
                        lines=lines_value,
                        level=level_value,
                        module=module_value,
                        range=range_value,
                        cursor=cursor_value,
                        page_size=page_size,
                        direction=direction_value,
                        request_id=request_filter,
                        search=search_value,
                    )
                except Exception as exc:
                    logger.warning(f"Failed to load logs snapshot: {exc}")
                    await _send_or_disconnect(
                        websocket,
                        {
                            "type": "logs_snapshot",
                            "success": False,
                            "error": str(exc),
                        },
                    )
                    continue

                payload = {
                    "type": "logs_snapshot",
                    "success": bool(result.get("success", True)),
                    "logs": result.get("logs") or [],
                    "pagination": result.get("pagination") or {},
                    "query": {
                        "range": range_value,
                        "module": module_value,
                        "level": level_value,
                        "direction": direction_value,
                        "cursor": cursor_value,
                        "page_size": page_size,
                        "request_id": request_filter,
                        "search": search_value,
                        "lines": lines_value,
                    },
                    "source": "websocket",
                }
                if request_token is not None:
                    payload["request_id"] = request_token
                last_responses[key] = payload
                await _send_or_disconnect(websocket, payload)
            elif msg_type == "request_positions_snapshot":
                key = "positions_snapshot"
                force = bool(message.get("force"))

                context = await _resolve_connection_context(health_payload, refresh=True)
                health_payload = context.health_payload
                connected_flag = context.connected
                account_type_label = context.account_type
                last_error_value = context.last_error
                redis_connected = context.redis_connected
                redis_fresh = context.redis_fresh
                health_error = context.health_error
                error_code_value = context.error_code

                _update_account_service_readiness(health_payload)

                if health_payload is None:
                    payload = _service_unavailable_payload(
                        "position_update",
                        positions=[],
                        source="websocket",
                    )
                    if health_error:
                        payload["error"] = health_error
                    if last_error_value:
                        payload["last_error"] = last_error_value
                    if error_code_value:
                        payload["error_code"] = error_code_value
                    last_responses[key] = payload
                    await websocket.send_text(json.dumps(payload))
                    continue

                use_cache = message.get("use_cache")
                use_cache = bool(use_cache) if use_cache is not None else False

                cached = last_responses.get(key)
                if use_cache and cached and not force:
                    cached_payload = dict(cached)
                    cached_payload["cached"] = True
                    await websocket.send_text(json.dumps(cached_payload))
                    continue

                if not _allow_request(request_tracker, key, force=force):
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        cached_payload["cached"] = True
                        await websocket.send_text(json.dumps(cached_payload))
                    else:
                        await websocket.send_text(json.dumps({
                            "type": "position_update",
                            "success": False,
                            "error": "Rate limited",
                            "source": "websocket",
                        }))
                    continue

                fast_flag = message.get("fast")
                fast = bool(fast_flag) if fast_flag is not None else False

                try:
                    snapshot = await read_positions_snapshot()
                    snapshot_metadata = (
                        _merge_metadata(snapshot)
                        if isinstance(snapshot, Mapping)
                        else {}
                    )
                    stale_snapshot = AccountServiceClient.is_metadata_stale(
                        snapshot_metadata
                    )
                    effective_force = force or stale_snapshot
                    positions_result = await _fetch_positions(
                        force_refresh=effective_force,
                        fast=fast,
                    )
                    unavailable_positions = (
                        _extract_service_unavailable_payload(
                            positions_result, "position_update"
                        )
                        if isinstance(positions_result, Mapping)
                        else None
                    )
                    if unavailable_positions is not None:
                        payload = dict(unavailable_positions)
                        if "positions" not in payload or not isinstance(
                            payload.get("positions"), list
                        ):
                            if isinstance(positions_snapshot_data, list):
                                payload["positions"] = [
                                    dict(item) if isinstance(item, Mapping) else item
                                    for item in positions_snapshot_data
                                ]
                            else:
                                payload["positions"] = []
                        payload.setdefault("source", "service")
                        payload["fast"] = bool(fast)
                        payload["force_refresh"] = bool(effective_force)
                        payload["connected"] = bool(connected_flag)
                        payload["fresh"] = bool(redis_fresh)
                        combined_meta = _merge_metadata(payload, snapshot_metadata)
                        if combined_meta.get("source") and not payload.get("source"):
                            payload["source"] = combined_meta["source"]
                        if (
                            "from_cache" not in payload
                            and combined_meta.get("from_cache") is not None
                        ):
                            payload["from_cache"] = bool(combined_meta.get("from_cache"))
                        if (
                            combined_meta.get("snapshot_updated_at")
                            and "snapshot_updated_at" not in payload
                        ):
                            payload["snapshot_updated_at"] = combined_meta[
                                "snapshot_updated_at"
                            ]
                        if combined_meta.get("stale_reason") and "stale_reason" not in payload:
                            payload["stale_reason"] = combined_meta["stale_reason"]
                        if combined_meta.get("stale_at") and "stale_at" not in payload:
                            payload["stale_at"] = combined_meta["stale_at"]
                        payload.setdefault(
                            "stale", AccountServiceClient.is_metadata_stale(combined_meta)
                        )
                        if combined_meta:
                            payload["metadata"] = dict(combined_meta)
                        last_responses[key] = payload
                        await websocket.send_text(json.dumps(payload))
                        continue

                    positions_payload, positions_metadata = _extract_positions_payload(
                        positions_result
                    )

                    source_value = positions_metadata.get("source")

                    if positions_payload is None:
                        if isinstance(snapshot, Mapping):
                            if not snapshot_metadata:
                                snapshot_metadata = _merge_metadata(snapshot)
                            if not source_value:
                                source_value = (
                                    snapshot.get("source") or IB_COMMAND_SOURCE
                                )
                            data = snapshot.get("data")
                            if isinstance(data, list):
                                positions_payload = [
                                    dict(item) if isinstance(item, Mapping) else item
                                    for item in data
                                ]

                    combined_meta = _merge_metadata(positions_metadata, snapshot_metadata)

                    if positions_payload is None:
                        unavailable_payload = _service_unavailable_payload(
                            "position_update",
                            positions=[],
                            source=combined_meta.get("source")
                            or source_value
                            or IB_COMMAND_SOURCE,
                            fast=fast,
                            force_refresh=bool(effective_force),
                            connected=bool(connected_flag),
                            fresh=bool(redis_fresh),
                        )
                        if combined_meta.get("from_cache") is not None:
                            unavailable_payload["from_cache"] = bool(
                                combined_meta.get("from_cache")
                            )
                        unavailable_payload.setdefault(
                            "details",
                            "Positions data unavailable from service or cache",
                        )
                        if (
                            combined_meta.get("snapshot_updated_at")
                            and "snapshot_updated_at" not in unavailable_payload
                        ):
                            unavailable_payload["snapshot_updated_at"] = combined_meta[
                                "snapshot_updated_at"
                            ]
                        if (
                            combined_meta.get("stale_reason")
                            and "stale_reason" not in unavailable_payload
                        ):
                            unavailable_payload["stale_reason"] = combined_meta[
                                "stale_reason"
                            ]
                        if (
                            combined_meta.get("stale_at")
                            and "stale_at" not in unavailable_payload
                        ):
                            unavailable_payload["stale_at"] = combined_meta["stale_at"]
                        if combined_meta:
                            unavailable_payload["metadata"] = dict(combined_meta)
                        last_responses[key] = unavailable_payload
                        await websocket.send_text(json.dumps(unavailable_payload))
                        continue

                    source_field = (
                        combined_meta.get("source")
                        or source_value
                        or IB_COMMAND_SOURCE
                    )
                    stale_flag = AccountServiceClient.is_metadata_stale(combined_meta)

                    payload = {
                        "type": "position_update",
                        "success": True,
                        "positions": positions_payload,
                        "source": source_field,
                        "fast": fast,
                        "connected": bool(connected_flag),
                        "fresh": bool(redis_fresh),
                        "stale": stale_flag,
                        "from_cache": bool(combined_meta.get("from_cache")),
                    }

                    if combined_meta.get("snapshot_updated_at"):
                        payload["snapshot_updated_at"] = combined_meta["snapshot_updated_at"]
                    if combined_meta.get("stale_reason"):
                        payload["stale_reason"] = combined_meta["stale_reason"]
                    if combined_meta.get("stale_at"):
                        payload["stale_at"] = combined_meta["stale_at"]

                    last_responses[key] = payload
                    await websocket.send_text(json.dumps(payload))
                except Exception as exc:
                    logger.warning(f"Failed to stream positions snapshot: {exc}")
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        cached_payload["cached"] = True
                        cached_payload["error"] = str(exc)
                        await websocket.send_text(json.dumps(cached_payload))
                    else:
                        error_payload = _service_unavailable_payload(
                            "position_update",
                            positions=[],
                            source="websocket",
                        )
                        error_payload["details"] = str(exc)
                        await websocket.send_text(json.dumps(error_payload))
            elif msg_type == "request_dom_stats":
                symbol = (message.get("symbol") or "").strip().upper()
                if not symbol:
                    await websocket.send_text(json.dumps({
                        "type": "dom_stats",
                        "success": False,
                        "error": "Symbol is required",
                    }))
                    continue

                try:
                    lookback_seconds = message.get("lookback_seconds")
                    lookback_val = int(lookback_seconds) if lookback_seconds is not None else None
                except (TypeError, ValueError):
                    lookback_val = None

                try:
                    max_points_raw = message.get("max_points")
                    max_points = int(max_points_raw) if max_points_raw is not None else None
                except (TypeError, ValueError):
                    max_points = None

                start_param = message.get("start")
                end_param = message.get("end")
                key = f"dom_stats:{symbol}:{lookback_val}:{max_points}:{start_param}:{end_param}"
                force = bool(message.get("force"))
                if not _allow_request(request_tracker, key, force=force):
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        cached_payload["cached"] = True
                        await websocket.send_text(json.dumps(cached_payload))
                    else:
                        await websocket.send_text(json.dumps({
                            "type": "dom_stats",
                            "success": False,
                            "symbol": symbol,
                            "error": "Rate limited",
                        }))
                    continue

                try:
                    result = await dom_client.get_stats(
                        symbol,
                        lookback_seconds=lookback_val,
                        start=start_param,
                        end=end_param,
                        max_points=max_points,
                    )
                    payload = {"type": "dom_stats", "success": True, "symbol": symbol}
                    if isinstance(result, dict):
                        payload.update(result)
                    last_responses[key] = payload
                    await websocket.send_text(json.dumps(payload))
                except Exception as exc:
                    logger.warning(f"Failed to fetch DOM stats: {exc}")
                    await websocket.send_text(json.dumps({
                        "type": "dom_stats",
                        "success": False,
                        "symbol": symbol,
                        "error": str(exc),
                    }))
            elif msg_type == "request_monitoring_queues":
                raw_queues = message.get("queues")
                if raw_queues is None:
                    raw_queues = message.get("queue")
                raw_streams = message.get("streams")
                if raw_streams is None:
                    raw_streams = message.get("stream")

                def _normalize_sequence(value: Any) -> list[str]:
                    if value is None:
                        return []
                    if isinstance(value, list):
                        return [str(item) for item in value if item not in (None, "")]
                    if isinstance(value, (tuple, set)):
                        return [str(item) for item in value if item not in (None, "")]
                    if isinstance(value, str):
                        token = value.strip()
                        return [token] if token else []
                    return [str(value)]

                queue_tokens = _normalize_sequence(raw_queues)
                stream_tokens = _normalize_sequence(raw_streams)
                key = "monitoring_queues:" + "|".join(
                    [";".join(queue_tokens) or "default", ";".join(stream_tokens) or "default"]
                )
                force = bool(message.get("force"))
                request_token = message.get("request_id")

                if not _allow_request(request_tracker, key, force=force):
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        cached_payload["cached"] = True
                        if request_token is not None:
                            cached_payload["request_id"] = request_token
                        await _send_or_disconnect(websocket, cached_payload)
                    else:
                        await _send_or_disconnect(
                            websocket,
                            {
                                "type": "monitoring_queues",
                                "success": False,
                                "error": "Rate limited",
                                "query": {
                                    "queues": queue_tokens,
                                    "streams": stream_tokens,
                                },
                            },
                        )
                    continue

                try:
                    data = await get_queue_metrics(
                        queues=queue_tokens or None,
                        streams=stream_tokens or None,
                    )
                except HTTPException as exc:
                    await _send_or_disconnect(
                        websocket,
                        {
                            "type": "monitoring_queues",
                            "success": False,
                            "error": getattr(exc, "detail", None) or str(exc),
                            "query": {
                                "queues": queue_tokens,
                                "streams": stream_tokens,
                            },
                        },
                    )
                    continue
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning(f"Failed to load monitoring queues: {exc}")
                    await _send_or_disconnect(
                        websocket,
                        {
                            "type": "monitoring_queues",
                            "success": False,
                            "error": str(exc),
                            "query": {
                                "queues": queue_tokens,
                                "streams": stream_tokens,
                            },
                        },
                    )
                    continue

                payload = {
                    "type": "monitoring_queues",
                    "success": True,
                    **data,
                }
                if request_token is not None:
                    payload["request_id"] = request_token
                last_responses[key] = payload
                await _send_or_disconnect(websocket, payload)
            elif msg_type == "request_metrics_write_rate":
                window = (message.get("window") or "1h").strip() or "1h"
                service_filter = message.get("service")
                metric_filter = message.get("metric")
                aggregate = (message.get("aggregate") or "sum").strip() or "sum"
                request_token = message.get("request_id")

                key = f"metrics_write_rate:{window}:{service_filter or ''}:{metric_filter or ''}:{aggregate}"
                force = bool(message.get("force"))
                if not _allow_request(request_tracker, key, force=force):
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        cached_payload["cached"] = True
                        if request_token is not None:
                            cached_payload["request_id"] = request_token
                        await _send_or_disconnect(websocket, cached_payload)
                    else:
                        await _send_or_disconnect(
                            websocket,
                            {
                                "type": "metrics_write_rate",
                                "success": False,
                                "error": "Rate limited",
                                "query": {
                                    "window": window,
                                    "service": service_filter,
                                    "metric": metric_filter,
                                    "aggregate": aggregate,
                                },
                            },
                        )
                    continue

                try:
                    data = await build_metric_series_payload(
                        window=window,
                        service=service_filter,
                        metric=metric_filter,
                        aggregate=aggregate,
                    )
                except HTTPException as exc:
                    await _send_or_disconnect(
                        websocket,
                        {
                            "type": "metrics_write_rate",
                            "success": False,
                            "error": getattr(exc, "detail", None) or str(exc),
                            "query": {
                                "window": window,
                                "service": service_filter,
                                "metric": metric_filter,
                                "aggregate": aggregate,
                            },
                        },
                    )
                    continue
                except Exception as exc:
                    logger.warning(f"Failed to load metrics write rate: {exc}")
                    await _send_or_disconnect(
                        websocket,
                        {
                            "type": "metrics_write_rate",
                            "success": False,
                            "error": str(exc),
                            "query": {
                                "window": window,
                                "service": service_filter,
                                "metric": metric_filter,
                                "aggregate": aggregate,
                            },
                        },
                    )
                    continue

                payload = {
                    "type": "metrics_write_rate",
                    "success": True,
                    "source": "websocket",
                    **data,
                }
                if request_token is not None:
                    payload["request_id"] = request_token
                last_responses[key] = payload
                await _send_or_disconnect(websocket, payload)
            elif msg_type == "request_risk_rules":
                key = "risk_rules"
                force = bool(message.get("force"))
                if not _allow_request(request_tracker, key, force=force):
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        cached_payload["cached"] = True
                        await websocket.send_text(json.dumps(cached_payload))
                    else:
                        await websocket.send_text(
                            json.dumps(
                                {
                                    "type": "risk_rules",
                                    "success": False,
                                    "error": "Rate limited",
                                }
                            )
                        )
                    continue

                try:
                    service_response = await _RISK_CLIENT.fetch_rules()
                except ServiceUnavailableError as exc:
                    logger.debug(f"Risk service unavailable: {exc}")
                    service_response = None
                except ServiceClientError as exc:
                    logger.warning(f"Risk service error: {exc}")
                    service_response = None

                rules_payload: Any = None
                source = "service"

                if isinstance(service_response, Mapping):
                    rules_payload = service_response.get("rules", service_response.get("data"))
                    if rules_payload is None:
                        rules_payload = service_response
                elif isinstance(service_response, list):
                    rules_payload = service_response

                if rules_payload is None:
                    payload = _service_unavailable_payload("risk_rules")
                    last_responses[key] = payload
                    await websocket.send_text(json.dumps(payload))
                    continue

                rules_payload = _normalise_rules_payload(rules_payload)

                payload = {
                    "type": "risk_rules",
                    "success": True,
                    "rules": rules_payload,
                    "source": source,
                }
                last_responses[key] = payload
                await websocket.send_text(json.dumps(payload))
            elif msg_type == "request_sizing_history":
                try:
                    limit = int(message.get("limit", 50))
                except (TypeError, ValueError):
                    limit = 50
                limit = max(1, min(limit, 500))

                symbol = (message.get("symbol") or "").strip() or None
                since_raw = message.get("since")
                context = message.get("context")

                key = f"sizing_history:{symbol or ''}:{since_raw or ''}:{limit}"
                force = bool(message.get("force"))
                if not _allow_request(request_tracker, key, force=force):
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        if context is not None:
                            cached_payload = dict(cached_payload)
                            cached_payload["context"] = context
                        cached_payload["cached"] = True
                        await _send_or_disconnect(websocket, cached_payload)
                    else:
                        await _send_or_disconnect(
                            websocket,
                            {
                                "type": "sizing_history",
                                "success": False,
                                "error": "Rate limited",
                                "context": context,
                            },
                        )
                    continue

                try:
                    response = await _RISK_CLIENT.fetch_sizing_history(
                        symbol=symbol,
                        since=since_raw,
                        limit=limit,
                    )
                except ServiceUnavailableError as exc:
                    logger.debug(f"Risk service unavailable for sizing history: {exc}")
                    response = None
                except ServiceClientError as exc:
                    logger.warning(f"Risk service error for sizing history: {exc}")
                    response = None

                base_payload: Dict[str, Any] | None = None
                if isinstance(response, Mapping):
                    base_payload = {"type": "sizing_history", **dict(response)}
                    data = base_payload.get("data")
                    if isinstance(data, list):
                        base_payload["data"] = _enrich_sizing_history_items(
                            [dict(item) if isinstance(item, Mapping) else item for item in data]
                        )
                elif isinstance(response, list):
                    base_payload = {
                        "type": "sizing_history",
                        "success": True,
                        "data": _enrich_sizing_history_items(
                            [dict(item) if isinstance(item, Mapping) else item for item in response]
                        ),
                    }

                if base_payload is None:
                    fallback = _service_unavailable_payload("sizing_history")
                    last_responses[key] = fallback
                    payload = dict(fallback)
                    if context is not None:
                        payload["context"] = context
                    await _send_or_disconnect(websocket, payload)
                    continue

                base_payload.setdefault("success", True)
                base_payload.setdefault("data", [])
                base_payload.setdefault(
                    "meta",
                    {
                        "limit": limit,
                        "symbol": symbol,
                        "since": since_raw,
                    },
                )
                last_responses[key] = base_payload
                payload = dict(base_payload)
                if context is not None:
                    payload["context"] = context
                await _send_or_disconnect(websocket, payload)
            elif msg_type == "request_strategies":
                key = "strategies_snapshot"
                force = bool(message.get("force"))
                if not _allow_request(request_tracker, key, force=force):
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        cached_payload["cached"] = True
                        await _send_or_disconnect(websocket, cached_payload)
                    else:
                        await _send_or_disconnect(
                            websocket,
                            {
                                "type": "strategies_snapshot",
                                "success": False,
                                "error": "Rate limited",
                            },
                        )
                    continue

                try:
                    response = await _STRATEGY_CLIENT.list_strategies()
                except ServiceUnavailableError as exc:
                    logger.debug(f"Strategy service unavailable: {exc}")
                    payload = _service_unavailable_payload("strategies_snapshot")
                    payload["detail"] = "Strategy service unavailable"
                    last_responses[key] = payload
                    await _send_or_disconnect(websocket, payload)
                    continue
                except ServiceClientError as exc:
                    logger.warning(f"Strategy service error: {exc}")
                    payload = _service_unavailable_payload("strategies_snapshot")
                    payload["detail"] = str(exc)
                    last_responses[key] = payload
                    await _send_or_disconnect(websocket, payload)
                    continue

                strategies: Any = []
                if isinstance(response, Mapping):
                    strategies = response.get("strategies")
                    if strategies is None:
                        strategies = response.get("data")
                    if strategies is None:
                        strategies = response
                elif isinstance(response, list):
                    strategies = response

                if not isinstance(strategies, list):
                    payload = _service_unavailable_payload("strategies_snapshot")
                    payload["detail"] = "Unexpected strategy payload"
                    last_responses[key] = payload
                    await _send_or_disconnect(websocket, payload)
                    continue

                payload = {
                    "type": "strategies_snapshot",
                    "success": True,
                    "strategies": strategies,
                    "source": "service",
                }
                last_responses[key] = payload
                await _send_or_disconnect(websocket, payload)
            elif msg_type == "request_strategy_status":
                name = (message.get("name") or "").strip()
                if not name:
                    await _send_or_disconnect(
                        websocket,
                        {
                            "type": "strategy_status",
                            "success": False,
                            "error": "Strategy name is required",
                        },
                    )
                    continue

                key = f"strategy_status:{name}"
                force = bool(message.get("force"))
                if not _allow_request(request_tracker, key, force=force):
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        cached_payload["cached"] = True
                        await _send_or_disconnect(websocket, cached_payload)
                    else:
                        await _send_or_disconnect(
                            websocket,
                            {
                                "type": "strategy_status",
                                "success": False,
                                "name": name,
                                "error": "Rate limited",
                            },
                        )
                    continue

                cached_status: Optional[Tuple[Dict[str, Any], str]] = None
                if not force:
                    try:
                        cached_status = await _STRATEGY_STATUS_STREAMER.get_cached_status(name)
                    except Exception as exc:  # pragma: no cover - defensive guard
                        logger.debug(
                            "Failed to load cached strategy status", strategy=name, error=str(exc)
                        )

                if cached_status:
                    status_payload, cache_source = cached_status
                    payload = {
                        "type": "strategy_status",
                        "success": True,
                        "name": name,
                        "status": status_payload,
                        "source": cache_source,
                    }
                    last_responses[key] = payload
                    await _send_or_disconnect(websocket, payload)
                    continue

                try:
                    response = await _STRATEGY_CLIENT.get_strategy_status(name)
                except ServiceUnavailableError as exc:
                    logger.debug(f"Strategy service unavailable for {name}: {exc}")
                    payload = _service_unavailable_payload("strategy_status", name=name)
                    payload["detail"] = "Strategy service unavailable"
                    last_responses[key] = payload
                    await _send_or_disconnect(websocket, payload)
                    continue
                except ServiceClientError as exc:
                    logger.warning(f"Strategy service error for {name}: {exc}")
                    payload = _service_unavailable_payload("strategy_status", name=name)
                    payload["detail"] = str(exc)
                    last_responses[key] = payload
                    await _send_or_disconnect(websocket, payload)
                    continue

                status_payload: Any = None
                if isinstance(response, Mapping):
                    status_payload = response.get("status")
                    if status_payload is None:
                        status_payload = response.get("data")
                    if status_payload is None:
                        status_payload = response

                if status_payload is None:
                    payload = _service_unavailable_payload("strategy_status", name=name)
                    payload["detail"] = "Unexpected strategy status payload"
                    last_responses[key] = payload
                    await _send_or_disconnect(websocket, payload)
                    continue

                if isinstance(status_payload, Mapping):
                    _STRATEGY_STATUS_STREAMER.remember_external_status(name, status_payload)

                payload = {
                    "type": "strategy_status",
                    "success": True,
                    "name": name,
                    "status": status_payload,
                    "source": "service",
                }
                last_responses[key] = payload
                await _send_or_disconnect(websocket, payload)
            elif msg_type == "request_strategy_performance":
                name = (message.get("name") or "").strip()
                if not name:
                    await _send_or_disconnect(
                        websocket,
                        {
                            "type": "strategy_performance",
                            "success": False,
                            "error": "Strategy name is required",
                        },
                    )
                    continue

                range_value = (message.get("range") or "1d").strip()
                start_raw = message.get("start")
                end_raw = message.get("end")
                start_date = parse_local_datetime(start_raw)
                end_date = parse_local_datetime(end_raw)

                if range_value:
                    try:
                        now = now_local()
                        if range_value == "1d":
                            start_date = now - timedelta(days=1)
                        elif range_value == "1w":
                            start_date = now - timedelta(weeks=1)
                        elif range_value == "1m":
                            start_date = now - timedelta(days=30)
                        elif range_value == "wtd":
                            start_date = now - timedelta(days=now.weekday())
                        elif range_value == "mtd":
                            start_date = now.replace(day=1)
                        elif range_value == "ytd":
                            start_date = now.replace(month=1, day=1)
                    except Exception:
                        pass

                key = f"strategy_performance:{name}:{range_value}:{start_raw}:{end_raw}"
                force = bool(message.get("force"))
                if not _allow_request(request_tracker, key, force=force):
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        cached_payload["cached"] = True
                        await _send_or_disconnect(websocket, cached_payload)
                    else:
                        await _send_or_disconnect(
                            websocket,
                            {
                                "type": "strategy_performance",
                                "success": False,
                                "name": name,
                                "range": range_value,
                                "error": "Rate limited",
                            },
                        )
                    continue

                cached_performance: Optional[Dict[str, Any]] = None
                if not force:
                    try:
                        cached_performance = await _STRATEGY_PERFORMANCE_CACHE.get_message(
                            name,
                            range_value,
                            start_raw,
                            end_raw,
                        )
                    except Exception as exc:  # pragma: no cover - defensive guard
                        logger.debug(
                            "Failed to load cached strategy performance",
                            strategy=name,
                            range=range_value,
                            error=str(exc),
                        )

                if cached_performance:
                    payload = dict(cached_performance)
                    last_responses[key] = payload
                    await _send_or_disconnect(websocket, payload)
                    continue

                if not force:
                    cache_stats: Dict[str, Any] = {}
                    try:
                        cache_stats = await cache_get_strategy_stats(name)
                    except Exception:
                        cache_stats = {}
                    cache_metrics: Dict[str, Any] = {}
                    if cache_stats and not cache_stats.get("stale"):
                        try:
                            cache_metrics = await cache_compute_performance(
                                name,
                                range_value=range_value,
                                start_date=start_date,
                                end_date=end_date,
                            )
                        except Exception:
                            cache_metrics = {}
                    if cache_metrics:
                        payload = {
                            "type": "strategy_performance",
                            "success": True,
                            "name": name,
                            "range": range_value,
                            "metrics": cache_metrics,
                            "source": "cache",
                        }
                        if cache_stats:
                            payload["stats"] = cache_stats
                        try:
                            await _STRATEGY_PERFORMANCE_CACHE.store_message(
                                name,
                                range_value,
                                start_raw,
                                end_raw,
                                payload,
                            )
                        except Exception as exc:  # pragma: no cover - defensive guard
                            logger.debug(
                                "Failed to cache computed strategy performance",
                                strategy=name,
                                range=range_value,
                                error=str(exc),
                            )
                        last_responses[key] = payload
                        await _send_or_disconnect(websocket, payload)
                        continue

                try:
                    response = await _STRATEGY_CLIENT.get_strategy_performance(
                        name,
                        range_=range_value,
                        start=start_raw,
                        end=end_raw,
                    )
                except ServiceUnavailableError as exc:
                    logger.debug(f"Strategy service unavailable for {name}: {exc}")
                    payload = _service_unavailable_payload(
                        "strategy_performance",
                        name=name,
                        range=range_value,
                    )
                    payload["detail"] = "Strategy service unavailable"
                    last_responses[key] = payload
                    await _send_or_disconnect(websocket, payload)
                    continue
                except ServiceClientError as exc:
                    logger.warning(f"Strategy service error for {name}: {exc}")
                    payload = _service_unavailable_payload(
                        "strategy_performance",
                        name=name,
                        range=range_value,
                    )
                    payload["detail"] = str(exc)
                    last_responses[key] = payload
                    await _send_or_disconnect(websocket, payload)
                    continue

                metrics: Any = None
                source = "service"
                if isinstance(response, Mapping):
                    metrics = (
                        response.get("metrics")
                        or response.get("performance")
                        or response.get("data")
                    )
                    if metrics is None:
                        nested = response.get("strategy")
                        if isinstance(nested, Mapping):
                            metrics = nested.get("metrics") or nested.get("performance")
                    if isinstance(response.get("source"), str):
                        source = response["source"] or source
                elif isinstance(response, list):
                    metrics = response

                if metrics is None:
                    payload = _service_unavailable_payload(
                        "strategy_performance",
                        name=name,
                        range=range_value,
                    )
                    payload["detail"] = "Unexpected strategy performance payload"
                    last_responses[key] = payload
                    await _send_or_disconnect(websocket, payload)
                    continue

                payload = {
                    "type": "strategy_performance",
                    "success": True,
                    "name": name,
                    "range": range_value,
                    "metrics": metrics,
                    "source": source,
                }
                try:
                    await _STRATEGY_PERFORMANCE_CACHE.store_message(
                        name,
                        range_value,
                        start_raw,
                        end_raw,
                        payload,
                    )
                except Exception as exc:  # pragma: no cover - defensive guard
                    logger.debug(
                        "Failed to store streamed strategy performance response",
                        strategy=name,
                        range=range_value,
                        error=str(exc),
                    )
                last_responses[key] = payload
                await _send_or_disconnect(websocket, payload)
    except WebSocketDisconnect:
        pass
    finally:
        for task, subscription in realtime_streams.values():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            if subscription is not None:
                await subscription.close()
        _websocket_manager().disconnect(websocket)


async def _forward_realtime_subscription(
    websocket: WebSocket, subscription: _RealtimeSubscriptionProtocol
) -> None:
    try:
        while True:
            message = await subscription.queue.get()
            await websocket.send_json(message)
    except asyncio.CancelledError:
        raise
    except WebSocketDisconnect:
        raise
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.debug("Realtime websocket forwarder stopped", error=str(exc))
