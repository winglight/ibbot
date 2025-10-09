from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from time import perf_counter
from typing import Any, AsyncIterator, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.account_snapshot_cache import read_account_info_snapshot, read_positions_snapshot
from src.account_snapshot_service import AccountSnapshotService, IB_COMMAND_SOURCE

try:  # pragma: no cover - exercised when ibapi is unavailable
    from services.account.ib_client import ServiceIBClient
except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency guard
    _IB_IMPORT_ERROR = exc

    class ServiceIBClient:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError(
                "Interactive Brokers API support is unavailable; install ibapi"
            ) from _IB_IMPORT_ERROR
from src.ibbot import update_service_health
from src.ibbot.logging import configure_service_logger
from src.ibbot.redis import get_redis_client
from src.ib.redis_streaming import (
    RedisPublisher,
    publish_account_event,
    publish_portfolio_event,
)
from src.ib.events import PortfolioPositionEvent
from src.observability.metrics import (
    build_metrics_router,
    record_ib_reconnect,
    set_subscription_gauge,
)
from src.routes.service_clients import (
    OrdersServiceClient,
    ServiceClientError,
    ServiceUnavailableError,
)
from src.utils import now_local, parse_local_datetime, to_local_iso
from src.utils.account_pnl_cache import (
    load_all_daily_totals,
    read_net_liquidation_snapshot,
)


ACCOUNT_SERVICE_NAME = "account-service"
logger = configure_service_logger(
    "ibbot.services.account",
    service_name="account",
    ib_client_id_env_var="ACCOUNT_IB_CLIENT_ID",
)
DEFAULT_HEALTH_INTERVAL = 30.0
DEFAULT_ORDER_LIMIT = 50
DAILY_SUMMARY_COMMAND_ENV = "ACCOUNT_DAILY_SUMMARY_COMMAND"
DEFAULT_DAILY_SUMMARY_COMMAND = "get_account_daily_summary"
DAILY_SUMMARY_TIMEOUT_ENV = "ACCOUNT_DAILY_SUMMARY_TIMEOUT"
DEFAULT_DAILY_SUMMARY_TIMEOUT = 5.0

DEFAULT_ACCOUNT_SUMMARY_TAGS: tuple[str, ...] = (
    "NetLiquidation",
    "TotalCashValue",
    "EquityWithLoanValue",
    "AvailableFunds",
)
DEFAULT_ACCOUNT_SUMMARY_TAG_STRING = ",".join(DEFAULT_ACCOUNT_SUMMARY_TAGS)


_ORDERS_CLIENT = OrdersServiceClient()


@dataclass
class AccountServiceState:
    """Container storing runtime state for the service."""

    redis: Any | None = None
    health_task: asyncio.Task | None = None
    watchdog_task: asyncio.Task | None = None
    watchdog_stop_event: asyncio.Event | None = None
    snapshot_service: AccountSnapshotService | None = None
    last_refresh_request: datetime | None = None
    health_interval: float = DEFAULT_HEALTH_INTERVAL
    stale: bool = False
    last_refresh_error: Optional[str] = None
    snapshot_updated_at: Optional[str] = None
    stale_reason: Optional[str] = None
    orders_last_refreshed_at: Optional[str] = None
    orders_source: Optional[str] = None
    orders_from_cache: Optional[bool] = None
    orders_stale: Optional[bool] = None
    orders_stale_reason: Optional[str] = None
    orders_last_error: Optional[str] = None
    orders_response_time_ms: Optional[float] = None
    daily_summary_last_refreshed_at: Optional[str] = None
    daily_summary_source: Optional[str] = None
    daily_summary_from_cache: Optional[bool] = None
    daily_summary_stale: Optional[bool] = None
    daily_summary_stale_reason: Optional[str] = None
    daily_summary_last_error: Optional[str] = None
    ib_client: ServiceIBClient | None = None
    publisher: RedisPublisher | None = None
    event_task: asyncio.Task | None = None
    shutdown_event: asyncio.Event | None = None
    subscription_lock: asyncio.Lock | None = None
    subscription_accounts: set[str] = field(default_factory=set)
    subscription_tags: set[str] = field(default_factory=set)
    subscription_count: int = 0

    def update_snapshot_metadata(
        self, *, info: Optional[Dict[str, Any]], positions: Optional[Dict[str, Any]]
    ) -> None:
        timestamps = []
        if info:
            updated_at = info.get("updated_at")
            if updated_at:
                timestamps.append(updated_at)
        if positions:
            updated_at = positions.get("updated_at")
            if updated_at:
                timestamps.append(updated_at)
        self.snapshot_updated_at = max(timestamps) if timestamps else None

    def as_metrics(
        self, *, info: Optional[Dict[str, Any]], positions: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        self.update_snapshot_metadata(info=info, positions=positions)
        metrics: Dict[str, Any] = {
            "last_refresh_requested": to_local_iso(self.last_refresh_request) or "",
            "account_snapshot_updated_at": (info or {}).get("updated_at", ""),
            "positions_snapshot_updated_at": (positions or {}).get("updated_at", ""),
            "snapshot_updated_at": self.snapshot_updated_at or "",
            "stale": self.stale,
            "last_refresh_error": self.last_refresh_error or "",
            "stale_reason": self.stale_reason or "",
        }
        metrics.update(
            {
                "orders_last_refreshed_at": self.orders_last_refreshed_at or "",
                "orders_source": self.orders_source or "",
                "orders_from_cache": "" if self.orders_from_cache is None else self.orders_from_cache,
                "orders_stale": "" if self.orders_stale is None else self.orders_stale,
                "orders_stale_reason": self.orders_stale_reason or "",
                "orders_last_error": self.orders_last_error or "",
                "orders_response_time_ms": ""
                if self.orders_response_time_ms is None
                else round(self.orders_response_time_ms, 3),
                "daily_summary_last_refreshed_at": self.daily_summary_last_refreshed_at or "",
                "daily_summary_source": self.daily_summary_source or "",
                "daily_summary_from_cache": ""
                if self.daily_summary_from_cache is None
                else self.daily_summary_from_cache,
                "daily_summary_stale": ""
                if self.daily_summary_stale is None
                else self.daily_summary_stale,
                "daily_summary_stale_reason": self.daily_summary_stale_reason or "",
                "daily_summary_last_error": self.daily_summary_last_error or "",
            }
        )
        return metrics


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    await _startup()
    try:
        yield
    finally:
        await _shutdown()


app = FastAPI(title="Account Service", lifespan=_lifespan)
app.include_router(build_metrics_router())
_state = AccountServiceState()


def _read_health_interval() -> float:
    raw = os.getenv("ACCOUNT_SERVICE_HEALTH_INTERVAL_SECONDS", "")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_HEALTH_INTERVAL
    return max(0.0, value)


async def _collect_snapshots() -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    info_snapshot = await read_account_info_snapshot()
    positions_snapshot = await read_positions_snapshot()
    return info_snapshot, positions_snapshot


def _normalize_accounts(accounts: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for account in accounts:
        if not account:
            continue
        cleaned = account.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        normalized.append(cleaned)
    return normalized


def _normalize_tags(tags: Sequence[str] | None) -> list[str]:
    if not tags:
        return list(DEFAULT_ACCOUNT_SUMMARY_TAGS)
    seen: set[str] = set()
    normalized: list[str] = []
    for tag in tags:
        if not tag:
            continue
        cleaned = tag.strip()
        if not cleaned:
            continue
        if cleaned in seen:
            continue
        seen.add(cleaned)
        normalized.append(cleaned)
    return normalized or list(DEFAULT_ACCOUNT_SUMMARY_TAGS)


def _parse_tags_query(raw: str | None) -> list[str]:
    if raw is None:
        return list(DEFAULT_ACCOUNT_SUMMARY_TAGS)
    parts = [segment.strip() for segment in raw.split(",")]
    return _normalize_tags(parts)


def _serialize_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return value


def _serialize_mapping(payload: Mapping[str, Any]) -> Dict[str, Any]:
    serialised: Dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, Mapping):
            serialised[str(key)] = _serialize_mapping(value)
        elif isinstance(value, (list, tuple)):
            serialised[str(key)] = [
                _serialize_mapping(item)
                if isinstance(item, Mapping)
                else _serialize_value(item)
                for item in value
            ]
        else:
            serialised[str(key)] = _serialize_value(value)
    return serialised


def _prepare_summary_payload(
    summary: Mapping[str, Any], accounts: Sequence[str]
) -> Dict[str, Any]:
    accounts_payload = summary.get("accounts")
    currencies_payload = summary.get("currencies")
    serialised_accounts = (
        _serialize_mapping(accounts_payload)
        if isinstance(accounts_payload, Mapping)
        else {}
    )
    serialised_currencies = (
        _serialize_mapping(currencies_payload)
        if isinstance(currencies_payload, Mapping)
        else {}
    )
    target_accounts = list(accounts) or list(serialised_accounts.keys())
    filtered_accounts = {
        account: serialised_accounts.get(account, {}) for account in target_accounts
    }
    filtered_currencies = {
        account: serialised_currencies.get(account, {}) for account in target_accounts
    }
    payload: Dict[str, Any] = {
        "request_id": summary.get("request_id"),
        "received_at": _serialize_value(summary.get("received_at")),
        "accounts": filtered_accounts,
        "currencies": filtered_currencies,
    }
    return payload


def _prepare_positions_payload(
    positions: Sequence[PortfolioPositionEvent], account: str
) -> list[Dict[str, Any]]:
    serialised: list[Dict[str, Any]] = []
    for position in positions:
        if position.account != account:
            continue
        data = position.model_dump()
        serialised.append(_serialize_mapping(data))
    return serialised


def _get_subscription_lock() -> asyncio.Lock:
    lock = _state.subscription_lock
    if lock is None:
        lock = asyncio.Lock()
        _state.subscription_lock = lock
    return lock


async def _ensure_publisher() -> RedisPublisher:
    publisher = _state.publisher
    if publisher is None:
        publisher = RedisPublisher(service_name="account")
        _state.publisher = publisher
    return publisher


async def _ensure_ib_client() -> ServiceIBClient:
    client = _state.ib_client
    if client is None:
        client = ServiceIBClient()
        _state.ib_client = client
    if not client.isConnected():
        await client.connect()
        record_ib_reconnect(service="account")
    snapshot_service = _state.snapshot_service
    if snapshot_service is not None:
        snapshot_service.set_ib_client(client)
    return client


async def _start_event_consumer() -> None:
    if _state.event_task is not None and not _state.event_task.done():
        return
    client = _state.ib_client
    publisher = _state.publisher
    if client is None or publisher is None:
        return
    _state.shutdown_event = asyncio.Event()
    _state.event_task = asyncio.create_task(_account_event_loop())


async def _stop_event_consumer() -> None:
    task = _state.event_task
    if task is None:
        return
    shutdown_event = _state.shutdown_event
    if shutdown_event is not None:
        shutdown_event.set()
    queue = None
    client = _state.ib_client
    if client is not None:
        try:
            queue = client.callback_queue
        except Exception:
            queue = None
    if queue is not None:
        try:
            await queue.put(("__shutdown__", None))
        except Exception:
            pass
    try:
        await asyncio.wait_for(task, timeout=5)
    except asyncio.TimeoutError:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.warning("Account event loop cancellation raised: %s", exc)
    except asyncio.CancelledError:  # pragma: no cover - expected cancellation path
        pass
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Account event loop shutdown error: %s", exc)
    _state.event_task = None
    _state.shutdown_event = None


EVENT_DISPATCH_TABLE: dict[str, Any] = {
    "account_update": publish_account_event,
    "account_summary": publish_account_event,
    "portfolio": publish_portfolio_event,
}


async def _dispatch_account_event(event_type: str, payload: Any) -> None:
    publisher = _state.publisher
    if publisher is None:
        return
    handler = EVENT_DISPATCH_TABLE.get(event_type)
    if handler is None:
        logger.debug("Ignoring unsupported account event '%s'", event_type)
        return
    try:
        await handler(publisher, payload)
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning(
            "Failed to publish account event %s to Redis: %s", event_type, exc
        )


async def _account_event_loop() -> None:
    client = _state.ib_client
    publisher = _state.publisher
    if client is None or publisher is None:
        return
    shutdown_event = _state.shutdown_event
    try:
        queue = client.callback_queue
    except Exception:
        logger.warning("Account client callback queue unavailable; event loop stopping")
        return
    while True:
        if shutdown_event is not None and shutdown_event.is_set():
            break
        try:
            event_type, payload = await asyncio.wait_for(queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:  # pragma: no cover - cancellation path
            break
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.warning("Account event loop read error: %s", exc)
            continue
        if event_type == "__shutdown__":
            break
        await _dispatch_account_event(event_type, payload)
def _resolve_daily_summary_command_method() -> str:
    return os.getenv(DAILY_SUMMARY_COMMAND_ENV, DEFAULT_DAILY_SUMMARY_COMMAND) or DEFAULT_DAILY_SUMMARY_COMMAND


def _resolve_daily_summary_timeout() -> float:
    raw = os.getenv(DAILY_SUMMARY_TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_DAILY_SUMMARY_TIMEOUT
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_DAILY_SUMMARY_TIMEOUT
    return max(0.0, value)


async def _ensure_redis_client() -> Any | None:
    client = _state.redis
    if client is not None:
        return client
    try:
        client = await get_redis_client()
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Failed to acquire Redis client: %s", exc)
        return None
    _state.redis = client
    return client


def _coerce_order_iterable(collection: Any) -> list[Dict[str, Any]]:
    if isinstance(collection, Mapping):
        iterable: Iterable[Any] = collection.values()
    elif isinstance(collection, Sequence) and not isinstance(collection, (str, bytes, bytearray)):
        iterable = collection
    elif isinstance(collection, Iterable) and not isinstance(collection, (str, bytes, bytearray)):
        iterable = collection
    else:
        return []

    result: list[Dict[str, Any]] = []
    for entry in iterable:
        if isinstance(entry, Mapping):
            result.append(dict(entry))
    return result


class AccountSubscriptionRequest(BaseModel):
    accounts: list[str]
    tags: list[str] | None = None


@app.post("/subscriptions/account")
async def create_account_subscription(
    payload: AccountSubscriptionRequest,
) -> Dict[str, Any]:
    accounts = _normalize_accounts(payload.accounts)
    if not accounts:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one account code is required",
        )
    tags = _normalize_tags(payload.tags)
    await _ensure_publisher()
    try:
        client = await _ensure_ib_client()
    except Exception as exc:
        logger.warning("Account client unavailable during subscription: %s", exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Account client unavailable",
        ) from exc
    await _start_event_consumer()
    try:
        await client.subscribe_account_updates(accounts)
        summary_raw = await client.fetch_account_summary(
            ",".join(tags) if tags else DEFAULT_ACCOUNT_SUMMARY_TAG_STRING
        )
    except Exception as exc:
        logger.warning("Failed to establish account subscription: %s", exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to establish account subscription",
        ) from exc
    summary_payload = _prepare_summary_payload(summary_raw, accounts)
    lock = _get_subscription_lock()
    async with lock:
        _state.subscription_count += 1
        _state.subscription_accounts.update(accounts)
        _state.subscription_tags.update(tags)
        subscription_count = _state.subscription_count
    set_subscription_gauge(
        service="account",
        channel="account-updates",
        count=subscription_count,
    )
    response: Dict[str, Any] = {
        "success": True,
        "accounts": accounts,
        "tags": tags,
        "subscription_count": subscription_count,
        "summary": summary_payload["accounts"],
        "currencies": summary_payload["currencies"],
        "request_id": summary_payload.get("request_id"),
        "received_at": summary_payload.get("received_at"),
    }
    return response


@app.delete("/subscriptions/account")
async def delete_account_subscription() -> Dict[str, Any]:
    lock = _get_subscription_lock()
    async with lock:
        if _state.subscription_count <= 0:
            set_subscription_gauge(
                service="account",
                channel="account-updates",
                count=0,
            )
            return {"success": True, "remaining_subscribers": 0}
        _state.subscription_count -= 1
        remaining = _state.subscription_count
        should_cleanup = remaining == 0
    set_subscription_gauge(
        service="account",
        channel="account-updates",
        count=remaining,
    )
    if should_cleanup:
        client = _state.ib_client
        if client is not None:
            try:
                await client.unsubscribe_account_updates()
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.warning("Account client unsubscribe failed: %s", exc)
            await _stop_event_consumer()
            try:
                await client.disconnect()
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.warning("Account client disconnect failed: %s", exc)
        _state.subscription_accounts.clear()
        _state.subscription_tags.clear()
    return {"success": True, "remaining_subscribers": remaining}


@app.get("/accounts/{account_id}/summary")
async def get_account_summary(
    account_id: str, tags: str | None = Query(None)
) -> Dict[str, Any]:
    accounts = _normalize_accounts([account_id])
    tag_values = _parse_tags_query(tags)
    await _ensure_publisher()
    try:
        client = await _ensure_ib_client()
    except Exception as exc:
        logger.warning("Account client unavailable for summary: %s", exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Account client unavailable",
        ) from exc
    await _start_event_consumer()
    try:
        summary_raw = await client.fetch_account_summary(
            ",".join(tag_values) if tag_values else DEFAULT_ACCOUNT_SUMMARY_TAG_STRING
        )
    except Exception as exc:
        logger.warning("Failed to fetch account summary for %s: %s", account_id, exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Account summary unavailable",
        ) from exc
    summary_payload = _prepare_summary_payload(summary_raw, accounts)
    account_summary = summary_payload["accounts"].get(account_id, {})
    account_currencies = summary_payload["currencies"].get(account_id, {})
    return {
        "account": account_id,
        "summary": account_summary,
        "currencies": account_currencies,
        "tags": tag_values,
        "request_id": summary_payload.get("request_id"),
        "received_at": summary_payload.get("received_at"),
    }


@app.get("/accounts/{account_id}/positions")
async def get_account_positions(account_id: str) -> Dict[str, Any]:
    await _ensure_publisher()
    try:
        client = await _ensure_ib_client()
    except Exception as exc:
        logger.warning("Account client unavailable for positions: %s", exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Account client unavailable",
        ) from exc
    await _start_event_consumer()
    try:
        positions = await client.fetch_positions()
    except Exception as exc:
        logger.warning("Failed to fetch positions for %s: %s", account_id, exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Account positions unavailable",
        ) from exc
    serialised_positions = _prepare_positions_payload(positions, account_id)
    return {
        "account": account_id,
        "positions": serialised_positions,
        "count": len(serialised_positions),
        "retrieved_at": _serialize_value(datetime.now(timezone.utc)),
    }


def _extract_orders(payload: Mapping[str, Any]) -> list[Dict[str, Any]]:
    orders_field = payload.get("orders")
    if isinstance(orders_field, Sequence) and not isinstance(orders_field, (str, bytes, bytearray)):
        return [dict(entry) for entry in orders_field if isinstance(entry, Mapping)]

    orders: list[Dict[str, Any]] = []
    orders.extend(_coerce_order_iterable(payload.get("active_orders")))
    orders.extend(_coerce_order_iterable(payload.get("history")))
    return orders


def _orders_metadata(payload: Mapping[str, Any]) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {
        "source": payload.get("source") or "orders-service",
        "snapshot_updated_at": payload.get("updated_at"),
        "stale": bool(payload.get("stale", False)),
        "stale_reason": payload.get("stale_reason"),
        "stale_at": payload.get("stale_at"),
    }
    return metadata


def _orders_payload_to_response(
    payload: Mapping[str, Any],
    *,
    limit: int,
    offset: int,
    from_cache: bool,
    source_override: Optional[str] = None,
) -> Tuple[list[Dict[str, Any]], int, Dict[str, Any]]:
    orders_all = _extract_orders(payload)
    total_raw = payload.get("total")
    try:
        total = int(total_raw)
    except (TypeError, ValueError):
        total = len(orders_all)

    metadata = _orders_metadata(payload)
    if source_override:
        metadata["source"] = source_override
    metadata["from_cache"] = from_cache

    sliced = orders_all[offset : offset + limit]
    return sliced, total, metadata


async def _fetch_orders_from_service(
    *, limit: int, offset: int, start_date: str | None = None
) -> Tuple[list[Dict[str, Any]], int, Dict[str, Any]]:
    start = perf_counter()
    try:
        payload = await _ORDERS_CLIENT.list_orders(
            limit=limit,
            offset=offset,
            start_date=start_date,
        )
    except ServiceUnavailableError as exc:
        detail = f"Orders service unavailable: {exc}"
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail) from exc
    except ServiceClientError as exc:
        detail = f"Orders service error: {exc}"
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail) from exc

    duration_ms = max((perf_counter() - start) * 1000.0, 0.0)

    if not isinstance(payload, Mapping):
        detail = "Invalid orders service response"
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)

    orders, total, metadata = _orders_payload_to_response(
        payload,
        limit=limit,
        offset=offset,
        from_cache=False,
        source_override="orders-service",
    )
    if payload.get("updated_at") and not metadata.get("snapshot_updated_at"):
        metadata["snapshot_updated_at"] = payload.get("updated_at")
    metadata.setdefault("source", "orders-service")
    metadata.setdefault("from_cache", False)
    metadata.setdefault("stale", False)
    metadata.setdefault("stale_reason", None)
    metadata["response_time_ms"] = round(duration_ms, 3)
    return orders, total, metadata


def _normalise_daily_summary_rows(totals: Mapping[str, Any] | None) -> list[Dict[str, Any]]:
    if not isinstance(totals, Mapping):
        return []

    rows: list[Dict[str, Any]] = []
    for day, metrics in totals.items():
        day_str = str(day)
        mapping = metrics if isinstance(metrics, Mapping) else {}
        pnl_raw = mapping.get("pnl", 0.0)
        orders_raw = mapping.get("orders", 0)
        try:
            pnl_value = float(pnl_raw)
        except (TypeError, ValueError):
            pnl_value = 0.0
        try:
            orders_value = int(orders_raw)
        except (TypeError, ValueError):
            try:
                orders_value = int(float(orders_raw))
            except (TypeError, ValueError):
                orders_value = 0
        rows.append({"date": day_str, "pnl": pnl_value, "orders": orders_value})

    rows.sort(key=lambda item: item["date"], reverse=True)
    return rows


async def _load_daily_summary_cache() -> Tuple[list[Dict[str, Any]], Optional[Dict[str, Any]], Dict[str, Any]]:
    totals = await load_all_daily_totals()
    net_liq_payload = await read_net_liquidation_snapshot()

    rows = _normalise_daily_summary_rows(totals if isinstance(totals, Mapping) else None)

    metadata: Dict[str, Any] = {
        "source": "daily-summary-cache",
        "snapshot_updated_at": None,
        "stale": False,
        "stale_reason": None,
        "from_cache": True,
    }

    net_liq: Optional[Dict[str, Any]] = None
    if isinstance(net_liq_payload, Mapping):
        net_liq = dict(net_liq_payload)
        metadata["source"] = net_liq.get("source") or metadata["source"]
        timestamp = net_liq.get("timestamp")
        if isinstance(timestamp, str) and timestamp:
            metadata["snapshot_updated_at"] = timestamp
    elif net_liq_payload is not None:
        metadata["stale"] = True
        metadata["stale_reason"] = "net-liq-invalid"
        metadata["from_cache"] = False

    if totals is None:
        metadata["stale"] = True
        metadata["stale_reason"] = "daily-summary-unavailable"
        metadata["from_cache"] = False

    if not rows and not net_liq:
        metadata["stale"] = True
        metadata.setdefault("stale_reason", "daily-summary-missing")
        metadata["from_cache"] = False

    return rows, net_liq, metadata


async def _publish_account_metrics() -> None:
    redis_client = await _ensure_redis_client()
    if redis_client is None:
        return

    try:
        info_snapshot, positions_snapshot = await _collect_snapshots()
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Failed to collect account snapshots for metrics update: %s", exc)
        info_snapshot = None
        positions_snapshot = None

    metrics = _state.as_metrics(info=info_snapshot, positions=positions_snapshot)

    try:
        await update_service_health(
            redis_client,
            ACCOUNT_SERVICE_NAME,
            healthy=None,
            last_error=_state.last_refresh_error or "",
            metrics=metrics,
            logger=logger,
        )
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Failed to publish account service metrics: %s", exc)


async def _record_orders_metrics(
    *,
    source: str,
    updated_at: Optional[str],
    from_cache: bool,
    stale: bool,
    stale_reason: Optional[str],
    error: Optional[str],
    response_time_ms: Optional[float],
) -> None:
    _state.orders_source = source
    _state.orders_last_refreshed_at = updated_at
    _state.orders_from_cache = from_cache
    _state.orders_stale = stale
    _state.orders_stale_reason = stale_reason
    _state.orders_last_error = error
    _state.orders_response_time_ms = response_time_ms
    await _publish_account_metrics()


async def _record_daily_summary_metrics(
    *,
    source: str,
    updated_at: Optional[str],
    from_cache: bool,
    stale: bool,
    stale_reason: Optional[str],
    error: Optional[str],
) -> None:
    _state.daily_summary_source = source
    _state.daily_summary_last_refreshed_at = updated_at
    _state.daily_summary_from_cache = from_cache
    _state.daily_summary_stale = stale
    _state.daily_summary_stale_reason = stale_reason
    _state.daily_summary_last_error = error
    await _publish_account_metrics()

def _parse_bool(value: Optional[bool | str], *, default: bool = False) -> bool:
    """Return a normalised boolean from FastAPI query parameters."""

    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        normalised = value.strip().lower()
        if normalised in {"1", "true", "t", "yes", "y", "on"}:
            return True
        if normalised in {"0", "false", "f", "no", "n", "off"}:
            return False
    return default


def _snapshot_is_stale(snapshot: Optional[Dict[str, Any]]) -> bool:
    if not snapshot or "data" not in snapshot:
        return True
    if snapshot.get("stale"):
        return True
    if snapshot.get("updated_at") is None:
        return True
    return False


def _snapshot_metadata(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    source = snapshot.get("source")
    if not source and snapshot.get("data"):
        source = IB_COMMAND_SOURCE
    return {
        "source": source,
        "snapshot_updated_at": snapshot.get("updated_at"),
        "stale": snapshot.get("stale", False),
        "stale_reason": snapshot.get("stale_reason"),
        "stale_at": snapshot.get("stale_at"),
    }


def _evaluate_snapshot_freshness(
    info_snapshot: Optional[Dict[str, Any]],
    positions_snapshot: Optional[Dict[str, Any]],
    *,
    refresh_interval: float,
    now: Optional[datetime] = None,
) -> Tuple[bool, Optional[str]]:
    if refresh_interval <= 0:
        return False, None

    reference = now or now_local()
    threshold = timedelta(seconds=refresh_interval * 2)
    stale_reasons: list[str] = []

    for label, snapshot in (
        ("account:info", info_snapshot),
        ("account:positions", positions_snapshot),
    ):
        if not snapshot:
            stale_reasons.append(f"{label} missing")
            continue
        updated_at = parse_local_datetime(snapshot.get("updated_at"))
        if updated_at is None:
            stale_reasons.append(f"{label} missing timestamp")
            continue
        if reference - updated_at > threshold:
            stale_reasons.append(f"{label} stale")

    if stale_reasons:
        return True, ", ".join(stale_reasons)
    return False, None


async def _health_publisher_loop() -> None:
    interval = _state.health_interval
    if interval <= 0:
        logger.info("Account service health publisher disabled (interval=%s)", interval)
        return

    logger.info("Starting account service health publisher (interval=%.1fs)", interval)

    while True:
        try:
            info_snapshot, positions_snapshot = await _collect_snapshots()
            metrics = _state.as_metrics(info=info_snapshot, positions=positions_snapshot)
            healthy = any((info_snapshot, positions_snapshot)) and not _state.stale
            redis_client = _state.redis
            if redis_client is not None:
                await update_service_health(
                    redis_client,
                    ACCOUNT_SERVICE_NAME,
                    healthy=healthy,
                    last_error=_state.last_refresh_error,
                    metrics=metrics,
                    logger=logger,
                )
        except asyncio.CancelledError:  # pragma: no cover - cooperative cancellation
            raise
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.warning("Failed to publish account service health: %s", exc)

        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:  # pragma: no cover - cooperative cancellation
            raise


async def _snapshot_watchdog_loop() -> None:
    service = _state.snapshot_service
    stop_event = _state.watchdog_stop_event
    if service is None or stop_event is None:
        logger.info("Account snapshot watchdog disabled (service unavailable)")
        return

    refresh_interval = float(service.refresh_interval_seconds)
    if refresh_interval <= 0:
        logger.info("Account snapshot watchdog disabled (refresh_interval=%s)", refresh_interval)
        return

    check_interval = max(5.0, min(refresh_interval, 60.0))
    logger.info(
        "Starting account snapshot watchdog (interval=%.1fs, threshold=%.1fs)",
        check_interval,
        refresh_interval * 2,
    )

    while not stop_event.is_set():
        try:
            info_snapshot, positions_snapshot = await _collect_snapshots()
        except asyncio.CancelledError:  # pragma: no cover - cooperative cancellation
            raise
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.warning("Watchdog failed to collect account snapshots: %s", exc)
            info_snapshot = None
            positions_snapshot = None

        stale, reason = _evaluate_snapshot_freshness(
            info_snapshot,
            positions_snapshot,
            refresh_interval=refresh_interval,
        )

        redis_client = _state.redis
        previously_stale = _state.stale

        if stale:
            _state.stale = True
            _state.last_refresh_error = reason or "snapshots stale"
            _state.stale_reason = reason or "snapshots stale"
            try:
                service.request_immediate_refresh(reason="watchdog-stale")
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.warning("Watchdog failed to request immediate refresh: %s", exc)
            if redis_client is not None:
                await update_service_health(
                    redis_client,
                    ACCOUNT_SERVICE_NAME,
                    healthy=False,
                    last_error=reason,
                    metrics=_state.as_metrics(
                        info=info_snapshot, positions=positions_snapshot
                    ),
                    logger=logger,
                )
        else:
            _state.stale = False
            _state.last_refresh_error = None
            _state.stale_reason = None
            if previously_stale and redis_client is not None:
                await update_service_health(
                    redis_client,
                    ACCOUNT_SERVICE_NAME,
                    healthy=True,
                    last_error="",
                    metrics=_state.as_metrics(
                        info=info_snapshot, positions=positions_snapshot
                    ),
                    logger=logger,
                )

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=check_interval)
            break
        except asyncio.TimeoutError:
            continue


async def _startup() -> None:
    _state.health_interval = _read_health_interval()
    _state.watchdog_stop_event = asyncio.Event()
    _state.stale = False
    _state.last_refresh_error = None
    _state.snapshot_updated_at = None
    _state.stale_reason = None
    _state.orders_last_refreshed_at = None
    _state.orders_source = None
    _state.orders_from_cache = None
    _state.orders_stale = None
    _state.orders_stale_reason = None
    _state.orders_last_error = None
    _state.daily_summary_last_refreshed_at = None
    _state.daily_summary_source = None
    _state.daily_summary_from_cache = None
    _state.daily_summary_stale = None
    _state.daily_summary_stale_reason = None
    _state.daily_summary_last_error = None
    _state.subscription_count = 0
    _state.subscription_accounts.clear()
    _state.subscription_tags.clear()
    _state.subscription_lock = asyncio.Lock()
    set_subscription_gauge(service="account", channel="account-updates", count=0)
    _state.ib_client = None
    _state.publisher = None
    _state.event_task = None
    _state.shutdown_event = None
    try:
        _state.redis = await get_redis_client()
    except Exception as exc:  # pragma: no cover - redis optional for local tests
        logger.warning("Failed to acquire Redis client: %s", exc)
        _state.redis = None

    _state.snapshot_service = AccountSnapshotService()
    service = _state.snapshot_service

    ib_client: ServiceIBClient | None = None
    try:
        ib_client = await _ensure_ib_client()
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Failed to initialise account IB client: %s", exc)
        ib_client = None

    if service is not None:
        service.set_ib_client(ib_client)
        await service.start()

    if _state.health_task is not None and not _state.health_task.done():
        _state.health_task.cancel()

    _state.health_task = asyncio.create_task(_health_publisher_loop())
    if _state.watchdog_task is not None and not _state.watchdog_task.done():
        _state.watchdog_task.cancel()

    _state.watchdog_task = asyncio.create_task(_snapshot_watchdog_loop())

    if _state.redis is not None:
        try:
            info_snapshot, positions_snapshot = await _collect_snapshots()
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.warning("Failed to collect account snapshots for initial health: %s", exc)
            info_snapshot = None
            positions_snapshot = None
        metrics = _state.as_metrics(info=info_snapshot, positions=positions_snapshot)
        healthy = any((info_snapshot, positions_snapshot)) and not _state.stale
        try:
            await update_service_health(
                _state.redis,
                ACCOUNT_SERVICE_NAME,
                healthy=healthy,
                last_error=_state.last_refresh_error,
                metrics=metrics,
                logger=logger,
            )
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.warning("Failed to publish initial account service health: %s", exc)

    logger.info("Account service startup complete")


async def _shutdown() -> None:
    if _state.watchdog_stop_event is not None:
        _state.watchdog_stop_event.set()

    task = _state.health_task
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:  # pragma: no cover - expected cancellation
            pass
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.warning("Account service health task shutdown error: %s", exc)
    _state.health_task = None

    watchdog_task = _state.watchdog_task
    if watchdog_task is not None:
        watchdog_task.cancel()
        try:
            await watchdog_task
        except asyncio.CancelledError:  # pragma: no cover - expected cancellation
            pass
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.warning("Account service watchdog shutdown error: %s", exc)
    _state.watchdog_task = None

    service = _state.snapshot_service
    if service is not None:
        try:
            await service.stop()
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.warning("Account snapshot service shutdown error: %s", exc)
    _state.snapshot_service = None
    _state.watchdog_stop_event = None
    await _stop_event_consumer()
    client = _state.ib_client
    if client is not None:
        try:
            await client.unsubscribe_account_updates()
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.debug(
                "Account client unsubscribe during shutdown failed: %s", exc
            )
        try:
            await client.disconnect()
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.warning("Account client disconnect failed during shutdown: %s", exc)
    _state.ib_client = None
    publisher = _state.publisher
    if publisher is not None:
        try:
            await publisher.close()
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.warning("Account publisher shutdown error: %s", exc)
    _state.publisher = None
    _state.subscription_accounts.clear()
    _state.subscription_tags.clear()
    _state.subscription_count = 0
    _state.subscription_lock = None
    set_subscription_gauge(service="account", channel="account-updates", count=0)
    logger.info("Account service shutdown complete")


def _snapshot_response(snapshot: Dict[str, Any], *, data_key: str) -> Dict[str, Any]:
    payload = {
        "success": True,
        data_key: snapshot.get("data"),
        "from_cache": True,
    }
    payload.update(_snapshot_metadata(snapshot))
    return payload


def _snapshot_requires_refresh(
    snapshot: Optional[Dict[str, Any]], *, force_refresh: bool
) -> bool:
    if force_refresh:
        return True
    if not snapshot or "data" not in snapshot:
        return True
    return bool(snapshot.get("stale"))


def _summarise_stale_state(
    info_snapshot: Optional[Dict[str, Any]],
    positions_snapshot: Optional[Dict[str, Any]],
) -> tuple[bool, Optional[str]]:
    stale = False
    reasons: list[str] = []

    for label, snapshot in (
        ("account-info", info_snapshot),
        ("positions", positions_snapshot),
    ):
        if not snapshot or "data" not in snapshot:
            stale = True
            reasons.append(f"{label}-missing")
            continue
        if snapshot.get("stale"):
            stale = True
            reason = snapshot.get("stale_reason") or f"{label}-stale"
            reasons.append(reason)

    return stale, ", ".join(reasons) if reasons else None


async def _publish_refresh_health(
    *,
    info_snapshot: Optional[Dict[str, Any]],
    positions_snapshot: Optional[Dict[str, Any]],
    error: Optional[str],
) -> None:
    stale, reason = _summarise_stale_state(info_snapshot, positions_snapshot)
    _state.stale = stale
    _state.last_refresh_error = error
    _state.stale_reason = reason

    metrics = _state.as_metrics(info=info_snapshot, positions=positions_snapshot)
    redis_client = _state.redis
    if redis_client is None:
        return

    healthy = error is None and not stale
    try:
        await update_service_health(
            redis_client,
            ACCOUNT_SERVICE_NAME,
            healthy=healthy,
            last_error=error or "",
            metrics=metrics,
            logger=logger,
        )
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Failed to publish account service health after refresh: %s", exc)


async def _refresh_account_info(
    *,
    force_refresh: bool,
    snapshot: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    current_snapshot = snapshot if snapshot is not None else await read_account_info_snapshot()
    needs_refresh = _snapshot_requires_refresh(current_snapshot, force_refresh=force_refresh)

    if needs_refresh:
        service = _state.snapshot_service
        reason = "api-force-refresh" if force_refresh else "api-stale"
        success = False
        error: Optional[str] = None

        if service is None:
            error = "snapshot-service-unavailable"
            logger.warning("Account info refresh skipped: snapshot service unavailable")
        else:
            try:
                await service.refresh_account_info(reason=reason)
                success = True
            except Exception as exc:
                error = str(exc)
                logger.warning("Account info refresh failed: %s", exc)

        info_snapshot, positions_snapshot = await _collect_snapshots()
        await _publish_refresh_health(
            info_snapshot=info_snapshot,
            positions_snapshot=positions_snapshot,
            error=None if success else error,
        )

        current_snapshot = info_snapshot
        metadata = _snapshot_metadata(current_snapshot) if current_snapshot else {}
        if success:
            logger.info(
                "Account info refresh succeeded (source=%s, updated_at=%s, stale=%s)",
                metadata.get("source"),
                metadata.get("snapshot_updated_at"),
                metadata.get("stale"),
            )
        else:
            logger.warning(
                "Account info refresh unsuccessful (error=%s, stale=%s, updated_at=%s)",
                error or "unknown-error",
                metadata.get("stale"),
                metadata.get("snapshot_updated_at"),
            )

    return await _ensure_snapshot(current_snapshot, kind="Account info")


async def _refresh_positions(
    *,
    force_refresh: bool,
    fast: bool,
    snapshot: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    current_snapshot = snapshot if snapshot is not None else await read_positions_snapshot()
    needs_refresh = _snapshot_requires_refresh(current_snapshot, force_refresh=force_refresh)

    if needs_refresh:
        service = _state.snapshot_service
        reason = "api-force-refresh" if force_refresh else "api-stale"
        success = False
        error: Optional[str] = None

        if service is None:
            error = "snapshot-service-unavailable"
            logger.warning("Positions refresh skipped: snapshot service unavailable")
        else:
            try:
                await service.refresh_positions(reason=reason, fast=fast)
                success = True
            except Exception as exc:
                error = str(exc)
                logger.warning("Positions refresh failed: %s", exc)

        info_snapshot, positions_snapshot = await _collect_snapshots()
        await _publish_refresh_health(
            info_snapshot=info_snapshot,
            positions_snapshot=positions_snapshot,
            error=None if success else error,
        )

        current_snapshot = positions_snapshot
        metadata = _snapshot_metadata(current_snapshot) if current_snapshot else {}
        if success:
            logger.info(
                "Positions refresh succeeded (source=%s, updated_at=%s, stale=%s)",
                metadata.get("source"),
                metadata.get("snapshot_updated_at"),
                metadata.get("stale"),
            )
        else:
            logger.warning(
                "Positions refresh unsuccessful (error=%s, stale=%s, updated_at=%s)",
                error or "unknown-error",
                metadata.get("stale"),
                metadata.get("snapshot_updated_at"),
            )

    return await _ensure_snapshot(current_snapshot, kind="Positions")


def _orders_response(
    orders: list[Dict[str, Any]],
    *,
    total: int,
    page: int,
    limit: int,
    metadata: Dict[str, Any],
) -> Dict[str, Any]:
    returned = len(orders)
    has_more = total > page * limit
    from_cache_flag = bool(metadata.get("from_cache", True))
    payload: Dict[str, Any] = {
        "success": True,
        "orders": orders,
        "meta": {
            "page": page,
            "limit": limit,
            "total": total,
            "returned": returned,
            "has_more": has_more,
        },
        "from_cache": from_cache_flag,
    }
    payload.update(metadata)
    return payload


async def _ensure_snapshot(snapshot: Optional[Dict[str, Any]], *, kind: str) -> Dict[str, Any]:
    if not snapshot or "data" not in snapshot:
        message = f"{kind} snapshot unavailable"
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=message)
    return snapshot


@app.get("/account/info")
async def get_account_info(
    force_refresh: bool | str | None = Query(False),
    fast: bool | str | None = Query(False),
) -> Dict[str, Any]:
    refresh = _parse_bool(force_refresh, default=False)
    _ = _parse_bool(fast, default=False)  # parsed for parity with other endpoints

    snapshot = await read_account_info_snapshot()
    snapshot = await _refresh_account_info(force_refresh=refresh, snapshot=snapshot)
    return _snapshot_response(snapshot, data_key="account_info")


@app.get("/account/positions")
async def get_account_positions(
    force_refresh: bool | str | None = Query(False),
    fast: bool | str | None = Query(False),
) -> Dict[str, Any]:
    refresh = _parse_bool(force_refresh, default=False)
    fast_flag = _parse_bool(fast, default=False)

    snapshot = await read_positions_snapshot()
    snapshot = await _refresh_positions(
        force_refresh=refresh,
        fast=fast_flag,
        snapshot=snapshot,
    )
    return _snapshot_response(snapshot, data_key="positions")


@app.get("/account/orders")
async def get_account_orders(
    force_refresh: str | None = Query(None),
    fast: str | None = Query(None),
    limit: int = Query(DEFAULT_ORDER_LIMIT, ge=1, le=500),
    page: int = Query(1, ge=1),
    offset: int | None = Query(None, ge=0),
) -> Dict[str, Any]:
    refresh = _parse_bool(force_refresh, default=False)
    fast_flag = _parse_bool(fast, default=True)

    if refresh:
        service = _state.snapshot_service
        if service is not None:
            try:
                await service.refresh_both(
                    reason="api-orders-force-refresh",
                    fast_positions=fast_flag,
                )
            except Exception as exc:
                logger.warning("Orders force refresh failed: %s", exc)

    if offset is not None:
        offset_value = offset
        if limit > 0:
            computed_page = (offset_value // limit) + 1
            if computed_page > 0:
                page = computed_page
    else:
        offset_value = (page - 1) * limit

    try:
        orders, total, metadata = await _fetch_orders_from_service(
            limit=limit,
            offset=offset_value,
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        reason = "orders-service-unavailable" if exc.status_code == status.HTTP_503_SERVICE_UNAVAILABLE else "orders-service-error"
        await _record_orders_metrics(
            source="orders-service",
            updated_at=None,
            from_cache=False,
            stale=True,
            stale_reason=reason,
            error=detail,
            response_time_ms=None,
        )
        raise

    metadata = dict(metadata)
    metadata.setdefault("from_cache", False)
    metadata.setdefault("source", "orders-service")

    if offset_value >= total:
        orders = []

    await _record_orders_metrics(
        source=str(metadata.get("source") or "orders-service"),
        updated_at=metadata.get("snapshot_updated_at"),
        from_cache=bool(metadata.get("from_cache", False)),
        stale=bool(metadata.get("stale", False)),
        stale_reason=metadata.get("stale_reason"),
        error=None,
        response_time_ms=metadata.get("response_time_ms"),
    )

    return _orders_response(
        orders,
        total=total,
        page=page,
        limit=limit,
        metadata=metadata,
    )


@app.get("/account/daily-summary")
async def get_daily_summary() -> Dict[str, Any]:
    rows, net_liq, metadata = await _load_daily_summary_cache()
    metadata = dict(metadata)

    has_data = bool(rows) or bool(net_liq)

    if not has_data:
        detail = "Daily summary data unavailable"
        metadata.setdefault("error", detail)
        metadata.setdefault("stale", True)
        metadata.setdefault("stale_reason", metadata.get("stale_reason") or "daily-summary-missing")
        await _record_daily_summary_metrics(
            source=str(metadata.get("source") or "daily-summary-unavailable"),
            updated_at=metadata.get("snapshot_updated_at"),
            from_cache=bool(metadata.get("from_cache", False)),
            stale=bool(metadata.get("stale", True)),
            stale_reason=metadata.get("stale_reason"),
            error=detail,
        )
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)

    response_metadata = dict(metadata)
    from_cache_flag = bool(response_metadata.pop("from_cache", True))
    response_metadata.setdefault("stale", False)

    payload: Dict[str, Any] = {
        "success": True,
        "daily_summary": rows,
        "net_liquidation": net_liq,
        "from_cache": from_cache_flag,
    }
    payload.update(response_metadata)

    await _record_daily_summary_metrics(
        source=str(payload.get("source") or "daily-summary-cache"),
        updated_at=payload.get("snapshot_updated_at"),
        from_cache=from_cache_flag,
        stale=bool(payload.get("stale", False)),
        stale_reason=payload.get("stale_reason"),
        error=None,
    )

    return payload


@app.post("/account/refresh")
async def post_refresh_request() -> JSONResponse:
    _state.last_refresh_request = now_local()
    requested_at = to_local_iso(_state.last_refresh_request)
    triggered = False
    error: Optional[str] = None

    service = _state.snapshot_service
    if service is not None:
        try:
            await service.refresh_both(
                reason="api-manual-refresh",
                fast_positions=True,
            )
            triggered = True
        except Exception as exc:  # pragma: no cover - refresh may fail in degraded mode
            error = str(exc)
            logger.warning("Failed to refresh account snapshots: %s", exc)
    else:
        error = "Account snapshot service unavailable"

    payload: Dict[str, Any] = {
        "success": triggered and error is None,
        "requested_at": requested_at,
        "triggered": triggered,
        "from_cache": False,
    }
    if error:
        payload["error"] = error
    return JSONResponse(status_code=status.HTTP_200_OK, content=payload)


__all__ = ["app"]
