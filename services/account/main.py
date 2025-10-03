from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, AsyncIterator, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse
from loguru import logger

from src.account_snapshot_cache import read_account_info_snapshot, read_positions_snapshot
from src.account_snapshot_service import AccountSnapshotService
from src.ibbot import update_service_health
from src.ibbot.redis import IBCommandError, IBCommandTimeout, get_redis_client, request_ib_command
from src.utils import now_local, parse_local_datetime, to_local_iso
from src.utils.account_pnl_cache import (
    load_all_daily_totals,
    read_net_liquidation_snapshot,
)


ACCOUNT_SERVICE_NAME = "account-service"
DEFAULT_HEALTH_INTERVAL = 30.0
RECENT_ORDERS_CACHE_KEY = "account:orders:recent"
DEFAULT_ORDER_LIMIT = 50
ORDERS_SNAPSHOT_KEY_ENV = "ACCOUNT_ORDERS_SNAPSHOT_KEY"
DEFAULT_ORDERS_SNAPSHOT_KEY = "json:orders:snapshot"
ORDERS_COMMAND_METHOD_ENV = "ACCOUNT_ORDERS_COMMAND"
DEFAULT_ORDERS_COMMAND_METHOD = "get_orders"
ORDERS_COMMAND_TIMEOUT_ENV = "ACCOUNT_ORDERS_COMMAND_TIMEOUT"
DEFAULT_ORDERS_COMMAND_TIMEOUT = 5.0
DAILY_SUMMARY_COMMAND_ENV = "ACCOUNT_DAILY_SUMMARY_COMMAND"
DEFAULT_DAILY_SUMMARY_COMMAND = "get_account_daily_summary"
DAILY_SUMMARY_TIMEOUT_ENV = "ACCOUNT_DAILY_SUMMARY_TIMEOUT"
DEFAULT_DAILY_SUMMARY_TIMEOUT = 5.0


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
    daily_summary_last_refreshed_at: Optional[str] = None
    daily_summary_source: Optional[str] = None
    daily_summary_from_cache: Optional[bool] = None
    daily_summary_stale: Optional[bool] = None
    daily_summary_stale_reason: Optional[str] = None
    daily_summary_last_error: Optional[str] = None

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


def _resolve_orders_snapshot_key() -> str:
    return os.getenv(ORDERS_SNAPSHOT_KEY_ENV, DEFAULT_ORDERS_SNAPSHOT_KEY) or DEFAULT_ORDERS_SNAPSHOT_KEY


def _resolve_orders_command_method() -> str:
    return os.getenv(ORDERS_COMMAND_METHOD_ENV, DEFAULT_ORDERS_COMMAND_METHOD) or DEFAULT_ORDERS_COMMAND_METHOD


def _resolve_orders_command_timeout() -> float:
    raw = os.getenv(ORDERS_COMMAND_TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_ORDERS_COMMAND_TIMEOUT
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_ORDERS_COMMAND_TIMEOUT
    return max(0.0, value)


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
        "source": payload.get("source") or "orders-snapshot",
        "snapshot_updated_at": payload.get("updated_at"),
        "stale": bool(payload.get("stale", False)),
        "stale_reason": payload.get("stale_reason"),
        "stale_at": payload.get("stale_at"),
    }
    return metadata


async def _read_orders_snapshot(client: Any) -> Optional[Dict[str, Any]]:
    key = _resolve_orders_snapshot_key()
    try:
        raw = await client.get(key)
    except Exception as exc:
        logger.warning("Failed to read orders snapshot %s: %s", key, exc)
        return None

    if not raw:
        return None

    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except Exception:
            raw = raw.decode("utf-8", errors="ignore")

    try:
        payload = json.loads(raw)
    except Exception as exc:
        logger.warning("Invalid orders snapshot payload: %s", exc)
        return None

    if isinstance(payload, Mapping):
        return dict(payload)
    return None


def _normalise_orders_command_result(result: Any) -> Optional[Dict[str, Any]]:
    if isinstance(result, Mapping):
        return dict(result)
    if isinstance(result, Sequence) and not isinstance(result, (str, bytes, bytearray)):
        orders = [dict(entry) for entry in result if isinstance(entry, Mapping)]
        return {"orders": orders}
    return None


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


async def _load_orders_snapshot_payload() -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    metadata: Dict[str, Any] = {
        "source": "orders-snapshot-miss",
        "snapshot_updated_at": None,
        "stale": True,
        "stale_reason": "orders-snapshot-missing",
        "stale_at": None,
        "from_cache": True,
    }

    client = await _ensure_redis_client()
    if client is None:
        metadata.update(
            {
                "source": "orders-snapshot-unavailable",
                "from_cache": False,
                "stale_reason": "redis-unavailable",
            }
        )
        return None, metadata

    payload = await _read_orders_snapshot(client)
    if payload is None:
        return None, metadata

    metadata = _orders_metadata(payload)
    metadata["from_cache"] = True
    return payload, metadata


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
) -> None:
    _state.orders_source = source
    _state.orders_last_refreshed_at = updated_at
    _state.orders_from_cache = from_cache
    _state.orders_stale = stale
    _state.orders_stale_reason = stale_reason
    _state.orders_last_error = error
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
    return {
        "source": snapshot.get("source"),
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
    try:
        _state.redis = await get_redis_client()
    except Exception as exc:  # pragma: no cover - redis optional for local tests
        logger.warning("Failed to acquire Redis client: %s", exc)
        _state.redis = None

    _state.snapshot_service = AccountSnapshotService()
    service = _state.snapshot_service

    if service is not None:
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


async def _load_orders_fast(
    *,
    limit: int,
    offset: int,
) -> Tuple[list[Dict[str, Any]], int, Dict[str, Any]]:
    metadata: Dict[str, Any] = {
        "source": "fast-cache",
        "snapshot_updated_at": None,
        "stale": False,
        "stale_reason": None,
        "stale_at": None,
        "from_cache": True,
    }

    client = _state.redis
    if client is None:
        try:
            client = await get_redis_client()
        except Exception as exc:
            metadata.update(
                {
                    "source": "fast-cache-unavailable",
                    "stale": True,
                    "stale_reason": str(exc),
                    "from_cache": False,
                }
            )
            return [], 0, metadata

    try:
        raw_payload = await client.get(RECENT_ORDERS_CACHE_KEY)
    except Exception as exc:
        logger.warning("Failed to read fast orders cache: %s", exc)
        metadata.update(
            {
                "source": "fast-cache-error",
                "stale": True,
                "stale_reason": str(exc),
                "from_cache": False,
            }
        )
        return [], 0, metadata

    if not raw_payload:
        metadata["source"] = "fast-cache-miss"
        metadata["from_cache"] = True
        return [], 0, metadata

    try:
        payload = json.loads(raw_payload)
    except Exception as exc:
        logger.warning("Invalid fast orders cache payload: %s", exc)
        metadata.update(
            {
                "source": "fast-cache-invalid",
                "stale": True,
                "stale_reason": "invalid-payload",
                "from_cache": False,
            }
        )
        return [], 0, metadata

    orders = payload.get("orders") if isinstance(payload, dict) else None
    if not isinstance(orders, list):
        orders = []

    metadata.update(
        {
            "source": payload.get("source") or metadata["source"],
            "snapshot_updated_at": payload.get("updated_at"),
            "stale": bool(payload.get("stale", False)),
            "stale_reason": payload.get("stale_reason"),
            "stale_at": payload.get("stale_at"),
        }
    )

    total = payload.get("total") if isinstance(payload, dict) else None
    if not isinstance(total, int):
        total = len(orders)

    sliced = orders[offset : offset + limit]
    return sliced, total, metadata


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

    offset = offset_value

    orders_payload, metadata = await _load_orders_snapshot_payload()
    metadata = dict(metadata)
    orders_snapshot = orders_payload
    fast_result: Tuple[list[Dict[str, Any]], int, Dict[str, Any]] | None = None

    if orders_snapshot is None and fast_flag:
        fast_result = await _load_orders_fast(limit=limit, offset=offset)

    if orders_snapshot is None:
        client = await _ensure_redis_client()

        if client is None:
            error_message = "Redis client unavailable for orders snapshot"
            metadata.setdefault("error", error_message)
            await _record_orders_metrics(
                source=str(metadata.get("source") or "orders-snapshot-unavailable"),
                updated_at=metadata.get("snapshot_updated_at"),
                from_cache=bool(metadata.get("from_cache", False)),
                stale=bool(metadata.get("stale", True)),
                stale_reason=metadata.get("stale_reason"),
                error=error_message,
            )
            if fast_result is not None:
                orders, total, fast_metadata = fast_result
                response_metadata = dict(fast_metadata)
                response_metadata.setdefault("error", error_message)
            else:
                orders, total = [], 0
                response_metadata = dict(metadata)
            if offset >= total:
                orders = []
            return _orders_response(
                orders,
                total=total,
                page=page,
                limit=limit,
                metadata=response_metadata,
            )

        method = _resolve_orders_command_method()
        timeout = _resolve_orders_command_timeout()
        try:
            command_result = await request_ib_command(method, redis=client, timeout=timeout)
        except IBCommandTimeout as exc:
            detail = "Timed out while requesting orders snapshot"
            await _record_orders_metrics(
                source=f"ib-command:{method}",
                updated_at=None,
                from_cache=False,
                stale=True,
                stale_reason="orders-command-timeout",
                error=detail,
            )
            raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=detail) from exc
        except IBCommandError as exc:
            detail = f"Orders command {method} failed: {exc}"
            await _record_orders_metrics(
                source=f"ib-command:{method}",
                updated_at=None,
                from_cache=False,
                stale=True,
                stale_reason="orders-command-error",
                error=detail,
            )
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail) from exc
        except Exception as exc:
            detail = f"Failed to execute orders command {method}: {exc}"
            await _record_orders_metrics(
                source=f"ib-command:{method}",
                updated_at=None,
                from_cache=False,
                stale=True,
                stale_reason="orders-command-error",
                error=detail,
            )
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail) from exc

        command_source = str(command_result.get("source") or f"ib-command:{method}")
        command_payload = _normalise_orders_command_result(command_result.get("result"))

        if command_payload is not None:
            orders_snapshot = command_payload
            metadata = _orders_metadata(command_payload)
            metadata["source"] = command_source
            metadata["from_cache"] = False
            updated_at = command_result.get("updated_at") or command_result.get("timestamp")
            if isinstance(updated_at, str) and updated_at:
                metadata.setdefault("snapshot_updated_at", updated_at)
        else:
            refreshed_payload, refreshed_metadata = await _load_orders_snapshot_payload()
            if refreshed_payload is not None:
                orders_snapshot = refreshed_payload
                metadata = dict(refreshed_metadata)
                metadata["from_cache"] = False
                metadata["source"] = command_source
            else:
                metadata = dict(refreshed_metadata)
                metadata["from_cache"] = False
                metadata["source"] = command_source
                metadata.setdefault("stale", True)
                metadata.setdefault("stale_reason", "orders-snapshot-missing")
                metadata.setdefault("error", "Orders snapshot unavailable after refresh")

    if orders_snapshot is None:
        fallback_metadata = dict(metadata)
        fallback_metadata["from_cache"] = False
        fallback_metadata.setdefault("stale", True)
        fallback_metadata.setdefault("from_cache", False)
        fallback_metadata.setdefault("stale_reason", fallback_metadata.get("stale_reason") or "orders-snapshot-missing")
        fallback_metadata.setdefault("error", fallback_metadata.get("error") or "Orders snapshot unavailable")

        if fast_result is not None:
            orders, total, fast_metadata = fast_result
            response_metadata = dict(fast_metadata)
            response_metadata.setdefault("stale", True)
            response_metadata.setdefault("stale_reason", fallback_metadata.get("stale_reason"))
            if fallback_metadata.get("error") and "error" not in response_metadata:
                response_metadata["error"] = fallback_metadata["error"]
        else:
            orders, total = [], 0
            response_metadata = fallback_metadata

        if offset >= total:
            orders = []

        await _record_orders_metrics(
            source=str(response_metadata.get("source") or fallback_metadata.get("source") or "orders-snapshot-miss"),
            updated_at=response_metadata.get("snapshot_updated_at"),
            from_cache=bool(response_metadata.get("from_cache", False)),
            stale=bool(response_metadata.get("stale", True)),
            stale_reason=response_metadata.get("stale_reason"),
            error=response_metadata.get("error"),
        )

        return _orders_response(
            orders,
            total=total,
            page=page,
            limit=limit,
            metadata=response_metadata,
        )

    response_orders, total, response_metadata = _orders_payload_to_response(
        orders_snapshot,
        limit=limit,
        offset=offset,
        from_cache=bool(metadata.get("from_cache", True)),
        source_override=metadata.get("source"),
    )

    # Merge supplementary metadata gathered while fetching the snapshot.
    if metadata.get("snapshot_updated_at") and not response_metadata.get("snapshot_updated_at"):
        response_metadata["snapshot_updated_at"] = metadata["snapshot_updated_at"]
    for key in ("stale", "stale_reason", "stale_at", "error"):
        if key in metadata and key not in response_metadata:
            response_metadata[key] = metadata[key]

    if offset >= total:
        response_orders = []

    await _record_orders_metrics(
        source=str(response_metadata.get("source") or metadata.get("source") or "orders-snapshot"),
        updated_at=response_metadata.get("snapshot_updated_at"),
        from_cache=bool(response_metadata.get("from_cache", True)),
        stale=bool(response_metadata.get("stale", False)),
        stale_reason=response_metadata.get("stale_reason"),
        error=None,
    )

    return _orders_response(
        response_orders,
        total=total,
        page=page,
        limit=limit,
        metadata=response_metadata,
    )


@app.get("/account/daily-summary")
async def get_daily_summary() -> Dict[str, Any]:
    rows, net_liq, metadata = await _load_daily_summary_cache()
    metadata = dict(metadata)

    has_data = bool(rows) or bool(net_liq)

    if not has_data:
        client = await _ensure_redis_client()
        if client is None:
            detail = "Daily summary unavailable: Redis client unavailable"
            metadata.setdefault("error", detail)
            await _record_daily_summary_metrics(
                source=str(metadata.get("source") or "daily-summary-unavailable"),
                updated_at=metadata.get("snapshot_updated_at"),
                from_cache=bool(metadata.get("from_cache", False)),
                stale=bool(metadata.get("stale", True)),
                stale_reason=metadata.get("stale_reason"),
                error=detail,
            )
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)

        method = _resolve_daily_summary_command_method()
        timeout = _resolve_daily_summary_timeout()
        try:
            command_result = await request_ib_command(method, redis=client, timeout=timeout)
        except IBCommandTimeout as exc:
            detail = "Timed out while requesting daily summary refresh"
            await _record_daily_summary_metrics(
                source=f"ib-command:{method}",
                updated_at=None,
                from_cache=False,
                stale=True,
                stale_reason="daily-summary-timeout",
                error=detail,
            )
            raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=detail) from exc
        except IBCommandError as exc:
            detail = f"Daily summary command {method} failed: {exc}"
            await _record_daily_summary_metrics(
                source=f"ib-command:{method}",
                updated_at=None,
                from_cache=False,
                stale=True,
                stale_reason="daily-summary-error",
                error=detail,
            )
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail) from exc
        except Exception as exc:
            detail = f"Failed to execute daily summary command {method}: {exc}"
            await _record_daily_summary_metrics(
                source=f"ib-command:{method}",
                updated_at=None,
                from_cache=False,
                stale=True,
                stale_reason="daily-summary-error",
                error=detail,
            )
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail) from exc

        command_source = str(command_result.get("source") or f"ib-command:{method}")
        rows, net_liq, refreshed_metadata = await _load_daily_summary_cache()
        if rows or net_liq:
            metadata = dict(refreshed_metadata)
            metadata["from_cache"] = False
            metadata["source"] = command_source
        else:
            detail = "Daily summary data unavailable after refresh"
            metadata = dict(refreshed_metadata)
            metadata["from_cache"] = False
            metadata["source"] = command_source
            metadata.setdefault("stale", True)
            metadata.setdefault("stale_reason", metadata.get("stale_reason") or "daily-summary-missing")
            metadata["error"] = detail
            await _record_daily_summary_metrics(
                source=metadata.get("source", command_source),
                updated_at=metadata.get("snapshot_updated_at"),
                from_cache=False,
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
    redis_client = _state.redis
    triggered = False
    error: Optional[str] = None

    if redis_client is not None:
        try:
            await request_ib_command("refresh_account", redis=redis_client, timeout=5)
            triggered = True
        except Exception as exc:  # pragma: no cover - optional command may fail
            error = str(exc)
            logger.warning("Failed to request account refresh: %s", exc)
    else:
        error = "Redis client unavailable"

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
