from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, AsyncIterator, Dict, List, Mapping, Optional, Tuple

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import JSONResponse
from loguru import logger

from src.account_snapshot_cache import (
    read_account_info_snapshot,
    read_positions_snapshot,
)
from src.account_snapshot_service import AccountSnapshotService
from src.ibbot import update_service_health
from src.ibbot.redis import get_redis_client, request_ib_command
from src.utils import (
    NY_TZ,
    ensure_local_timezone,
    now_local,
    parse_local_datetime,
    to_local_iso,
)
from src.utils.account_pnl_cache import (
    load_all_daily_totals,
    read_net_liquidation_snapshot,
)


ACCOUNT_SERVICE_NAME = "account-service"
DEFAULT_HEALTH_INTERVAL = 30.0


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
        }
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


_UNSET = object()


def _snapshot_needs_refresh(
    snapshot: Optional[Mapping[str, Any]],
    *,
    refresh_interval: float,
) -> bool:
    if not snapshot:
        return True
    data = snapshot.get("data") if isinstance(snapshot, Mapping) else None
    if data is None:
        return True
    if bool(snapshot.get("stale")):
        return True
    if _state.stale:
        return True
    if refresh_interval <= 0:
        return False
    updated_at = parse_local_datetime(snapshot.get("updated_at"))
    if updated_at is None:
        return True
    age = now_local() - updated_at
    return age > timedelta(seconds=refresh_interval * 2)


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


def _snapshot_response(
    snapshot: Mapping[str, Any],
    *,
    data_key: str,
    from_cache: bool = True,
    data_override: Any = _UNSET,
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    data_value = snapshot.get("data")
    if data_override is not _UNSET:
        data_value = data_override
    payload: Dict[str, Any] = {
        "success": True,
        data_key: data_value,
        "from_cache": from_cache,
        "cache_updated_at": snapshot.get("updated_at"),
        "cache_source": snapshot.get("source"),
        "stale": snapshot.get("stale", False),
        "stale_reason": snapshot.get("stale_reason"),
        "stale_at": snapshot.get("stale_at"),
    }
    if extra:
        payload.update(dict(extra))
    return payload


async def _ensure_snapshot(snapshot: Optional[Dict[str, Any]], *, kind: str) -> Dict[str, Any]:
    if not snapshot or "data" not in snapshot:
        message = f"{kind} snapshot unavailable"
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=message)
    return snapshot


async def _read_orders_snapshot() -> Optional[Dict[str, Any]]:
    redis_client = _state.redis
    if redis_client is None:
        try:
            redis_client = await get_redis_client()
            _state.redis = redis_client
        except Exception as exc:  # pragma: no cover - redis optional in tests
            logger.debug("Orders snapshot Redis unavailable: %s", exc)
            return None

    try:
        raw = await redis_client.get("json:orders:snapshot")
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Failed to read orders snapshot: %s", exc)
        return None

    if not raw:
        return None

    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except Exception:
            raw = raw.decode("utf-8", errors="ignore")

    try:
        payload = json.loads(raw)
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Invalid orders snapshot payload: %s", exc)
        return None

    if isinstance(payload, dict):
        return payload
    return None


@app.get("/account/info")
async def get_account_info(force_refresh: bool = False) -> Dict[str, Any]:
    service = _state.snapshot_service
    refresh_interval = (
        float(getattr(service, "refresh_interval_seconds", 0) or 0.0)
        if service is not None
        else 0.0
    )

    initial_snapshot = await read_account_info_snapshot()
    should_refresh = force_refresh or _snapshot_needs_refresh(
        initial_snapshot, refresh_interval=refresh_interval
    )

    refresh_attempted = False
    refresh_succeeded = False
    refresh_error: Optional[str] = None
    refresh_reason = "force" if force_refresh else "stale"

    if should_refresh:
        if service is None:
            refresh_error = "snapshot service unavailable"
        else:
            refresh_attempted = True
            try:
                await service.refresh_account_info(
                    reason="api_force_refresh" if force_refresh else "api_auto_refresh"
                )
                refresh_succeeded = True
                _state.stale = False
                _state.last_refresh_error = None
            except Exception as exc:  # pragma: no cover - defensive guard
                refresh_error = str(exc)
                _state.last_refresh_error = refresh_error
                logger.warning("Account info refresh failed: %s", exc)

    latest_snapshot = await read_account_info_snapshot()
    if latest_snapshot is None and initial_snapshot is not None:
        latest_snapshot = initial_snapshot

    snapshot = await _ensure_snapshot(latest_snapshot, kind="Account info")

    extra: Dict[str, Any] = {
        "refresh_requested": bool(force_refresh),
        "refresh_attempted": refresh_attempted,
        "refresh_reason": refresh_reason if should_refresh else None,
        "refresh_succeeded": refresh_succeeded,
    }
    if refresh_error:
        extra["refresh_error"] = refresh_error

    return _snapshot_response(
        snapshot,
        data_key="account_info",
        from_cache=not refresh_succeeded,
        extra=extra,
    )


@app.get("/account/positions")
async def get_account_positions(
    fast: bool = True, force_refresh: bool = False
) -> Dict[str, Any]:
    service = _state.snapshot_service
    refresh_interval = (
        float(getattr(service, "refresh_interval_seconds", 0) or 0.0)
        if service is not None
        else 0.0
    )

    initial_snapshot = await read_positions_snapshot()
    should_refresh = force_refresh or _snapshot_needs_refresh(
        initial_snapshot, refresh_interval=refresh_interval
    )

    refresh_attempted = False
    refresh_succeeded = False
    refresh_error: Optional[str] = None
    refresh_reason = "force" if force_refresh else "stale"

    if should_refresh:
        if service is None:
            refresh_error = "snapshot service unavailable"
        else:
            refresh_attempted = True
            try:
                await service.refresh_positions(
                    reason="api_force_refresh" if force_refresh else "api_auto_refresh",
                    fast=fast,
                )
                refresh_succeeded = True
                _state.stale = False
                _state.last_refresh_error = None
            except Exception as exc:  # pragma: no cover - defensive guard
                refresh_error = str(exc)
                _state.last_refresh_error = refresh_error
                logger.warning("Positions refresh failed: %s", exc)

    latest_snapshot = await read_positions_snapshot()
    if latest_snapshot is None and initial_snapshot is not None:
        latest_snapshot = initial_snapshot

    snapshot = await _ensure_snapshot(latest_snapshot, kind="Positions")

    extra: Dict[str, Any] = {
        "fast": bool(fast),
        "refresh_requested": bool(force_refresh),
        "refresh_attempted": refresh_attempted,
        "refresh_reason": refresh_reason if should_refresh else None,
        "refresh_succeeded": refresh_succeeded,
    }
    if refresh_error:
        extra["refresh_error"] = refresh_error

    return _snapshot_response(
        snapshot,
        data_key="positions",
        from_cache=not refresh_succeeded,
        extra=extra,
    )


@app.get("/account/orders")
async def get_account_orders(
    limit: int = 50,
    offset: int = 0,
    start_date: Optional[str] = None,
) -> Dict[str, Any]:
    snapshot = await _read_orders_snapshot()
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Orders snapshot unavailable",
        )

    active_entries: List[Any] = []
    raw_active = snapshot.get("active_orders")
    if isinstance(raw_active, Mapping):
        active_entries = list(raw_active.values())
    elif isinstance(raw_active, list):
        active_entries = raw_active
    elif snapshot.get("orders") is not None:
        fallback_orders = snapshot.get("orders")
        if isinstance(fallback_orders, list):
            active_entries = fallback_orders

    active_orders: List[Dict[str, Any]] = []
    for entry in active_entries:
        if isinstance(entry, Mapping):
            active_orders.append(dict(entry))

    history_entries = snapshot.get("history")
    history_orders: List[Dict[str, Any]] = []
    if isinstance(history_entries, list):
        for entry in history_entries:
            if isinstance(entry, Mapping):
                history_orders.append(dict(entry))

    total_history = len(history_orders)
    safe_offset = max(0, int(offset))
    safe_limit = max(0, int(limit)) if limit is not None else 0
    if safe_limit == 0:
        paginated_history = history_orders[safe_offset:]
    else:
        paginated_history = history_orders[safe_offset : safe_offset + safe_limit]

    combined_orders = list(active_orders) + paginated_history
    total_orders = len(active_orders) + total_history

    meta: Dict[str, Any] = {
        "total": total_orders,
        "active": len(active_orders),
        "history_total": total_history,
        "offset": safe_offset,
        "limit": safe_limit,
    }
    if start_date:
        meta["start_date"] = start_date

    payload: Dict[str, Any] = {
        "success": True,
        "orders": combined_orders,
        "active_orders": active_orders,
        "history": paginated_history,
        "from_cache": True,
        "cache_updated_at": snapshot.get("updated_at"),
        "cache_source": snapshot.get("source"),
        "meta": meta,
    }
    return payload


@app.get("/account/daily-summary")
async def get_daily_summary(days: int = 30) -> Dict[str, Any]:
    if days < 1 or days > 365:
        raise HTTPException(status_code=400, detail="days must be between 1 and 365")

    ny_now = datetime.now(NY_TZ)
    end_date = ny_now.date()
    start_date = end_date - timedelta(days=days - 1)

    cache_data = await load_all_daily_totals()
    if not cache_data:
        detail_message = "暂无Redis每日盈余缓存，请刷新账户快照后重试。"
        refresh_hint = "请在账户服务恢复后触发刷新或等待定时任务写入缓存。"
        return {
            "success": False,
            "detail": detail_message,
            "refresh_hint": refresh_hint,
            "days": [],
            "range": {
                "start": start_date.isoformat(),
                "end": end_date.isoformat(),
            },
            "net_liquidation": None,
            "net_liquidation_timestamp": None,
            "base_surplus": None,
            "surplus_source": "unknown",
            "timezone": "America/New_York",
            "from_cache": True,
        }

    daily_stats: Dict[date, Dict[str, float]] = {}
    for day_str, metrics in cache_data.items():
        try:
            trade_day = date.fromisoformat(str(day_str))
        except (TypeError, ValueError):
            continue
        if trade_day < start_date or trade_day > end_date:
            continue
        pnl_raw = metrics.get("pnl", 0.0) if isinstance(metrics, Mapping) else 0.0
        orders_raw = metrics.get("orders", 0) if isinstance(metrics, Mapping) else 0
        try:
            pnl_value = float(pnl_raw)
        except (TypeError, ValueError):
            pnl_value = 0.0
        try:
            orders_value = int(float(orders_raw))
        except (TypeError, ValueError):
            orders_value = 0
        daily_stats[trade_day] = {"pnl": pnl_value, "orders": orders_value}

    ordered_dates: List[date] = []
    current = start_date
    while current <= end_date:
        ordered_dates.append(current)
        current += timedelta(days=1)

    net_cache = await read_net_liquidation_snapshot()
    net_liquidation: Optional[float] = None
    net_liquidation_timestamp: Optional[str] = None
    surplus_source = "unknown"

    if isinstance(net_cache, Mapping):
        raw_value = net_cache.get("value")
        try:
            if raw_value is not None:
                net_liquidation = float(raw_value)
                surplus_source = net_cache.get("source") or "redis"
        except (TypeError, ValueError):
            net_liquidation = None
        ts_raw = net_cache.get("timestamp")
        if isinstance(ts_raw, datetime):
            net_liquidation_timestamp = ensure_local_timezone(ts_raw).isoformat()
        elif isinstance(ts_raw, str):
            parsed = parse_local_datetime(ts_raw)
            if parsed is not None:
                net_liquidation_timestamp = parsed.isoformat()
            else:
                net_liquidation_timestamp = ts_raw

    account_snapshot = await read_account_info_snapshot()
    account_data = (
        account_snapshot.get("data")
        if isinstance(account_snapshot, Mapping)
        else None
    )
    account_updated_at = None
    if isinstance(account_snapshot, Mapping):
        account_updated_at = account_snapshot.get("updated_at")

    def _resolve_net_from_account(info: Mapping[str, Any] | None) -> Optional[float]:
        if not isinstance(info, Mapping):
            return None
        for key in ("net_liquidation", "netLiquidation", "net_liquidation_value"):
            value = info.get(key)
            if value is None:
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return None

    if net_liquidation is None:
        resolved = _resolve_net_from_account(account_data)
        if resolved is not None:
            net_liquidation = resolved
            surplus_source = "ibkr_cached"
            if isinstance(account_updated_at, str):
                parsed = parse_local_datetime(account_updated_at)
                if parsed is not None:
                    net_liquidation_timestamp = parsed.isoformat()
                else:
                    net_liquidation_timestamp = account_updated_at
            elif isinstance(account_updated_at, datetime):
                net_liquidation_timestamp = ensure_local_timezone(
                    account_updated_at
                ).isoformat()

    total_pnl = sum(
        float(daily_stats.get(day, {}).get("pnl", 0.0)) for day in ordered_dates
    )
    base_surplus: Optional[float] = None
    if net_liquidation is not None:
        base_surplus = float(net_liquidation) - float(total_pnl)

    summary: List[Dict[str, Any]] = []
    running_surplus = base_surplus
    for trade_day in ordered_dates:
        stats = daily_stats.get(trade_day, {"pnl": 0.0, "orders": 0})
        pnl_value = stats.get("pnl", 0.0) or 0.0
        orders_value = stats.get("orders", 0) or 0
        try:
            pnl_float = float(pnl_value)
        except (TypeError, ValueError):
            pnl_float = 0.0
        try:
            orders_int = int(orders_value)
        except (TypeError, ValueError):
            orders_int = 0

        if running_surplus is None:
            start_val = None
            end_val = None
        else:
            start_val = running_surplus
            end_val = running_surplus + pnl_float
            running_surplus = end_val

        summary.append(
            {
                "date": trade_day.isoformat(),
                "orders": orders_int,
                "has_orders": orders_int > 0,
                "realized_pnl": round(pnl_float, 2),
                "starting_surplus": round(start_val, 2)
                if start_val is not None
                else None,
                "ending_surplus": round(end_val, 2) if end_val is not None else None,
            }
        )

    return {
        "success": True,
        "days": summary,
        "range": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        "net_liquidation": float(net_liquidation)
        if net_liquidation is not None
        else None,
        "net_liquidation_timestamp": net_liquidation_timestamp,
        "base_surplus": round(base_surplus, 2) if base_surplus is not None else None,
        "surplus_source": surplus_source,
        "timezone": "America/New_York",
        "from_cache": True,
    }


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
