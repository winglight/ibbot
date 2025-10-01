import asyncio
from datetime import date, datetime, timedelta
from typing import Optional, Dict, List, Any

import os

import httpx
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import JSONResponse
from loguru import logger

from src.utils import now_local, parse_local_datetime, NY_TZ, ensure_local_timezone
from src.account_snapshot_cache import (
    read_account_info_snapshot,
    read_positions_snapshot,
    write_account_info_snapshot,
    write_positions_snapshot,
)
from src.utils.account_pnl_cache import (
    load_all_daily_totals,
    read_net_liquidation_snapshot,
    rebuild_account_daily_cache,
    write_net_liquidation_snapshot,
)

import core
from core import connection_manager
from src.database import get_db_manager
from .auth import get_current_user

router = APIRouter()

ACCOUNT_SERVICE_URL = os.getenv("ACCOUNT_SERVICE_URL", "http://localhost:8020").rstrip("/")
ACCOUNT_SERVICE_TIMEOUT = float(os.getenv("ACCOUNT_SERVICE_TIMEOUT", "10.0"))


def _compute_cache_age(updated_at: Optional[str]) -> Optional[float]:
    dt = parse_local_datetime(updated_at)
    if not dt:
        return None
    try:
        delta = now_local() - dt
        seconds = delta.total_seconds()
    except Exception:
        return None
    return max(0.0, seconds)


def _snapshot_response(
    snapshot: Optional[Dict[str, Any]],
    *,
    data_key: str,
    connected: bool,
    from_cache: bool,
    warning: Optional[str] = None,
    data_override: Any = None,
    default: Any = None,
) -> Dict[str, Any]:
    data = default
    if data_override is not None:
        data = data_override
    elif snapshot is not None and "data" in snapshot:
        data = snapshot.get("data")

    response: Dict[str, Any] = {
        "success": True,
        data_key: data if data is not None else default,
        "from_cache": from_cache,
        "connected": connected,
        "cache_updated_at": snapshot.get("updated_at") if snapshot else None,
        "cache_age_seconds": _compute_cache_age(
            snapshot.get("updated_at") if snapshot else None
        ),
        "cache_source": snapshot.get("source") if snapshot else None,
        "stale": snapshot.get("stale", False) if snapshot else False,
    }

    if snapshot and snapshot.get("stale_reason"):
        response["stale_reason"] = snapshot.get("stale_reason")
    if snapshot and snapshot.get("stale_at"):
        response["stale_at"] = snapshot.get("stale_at")
    if warning:
        response["warning"] = warning

    return response


def _account_service_base_url() -> str:
    return ACCOUNT_SERVICE_URL or "http://localhost:8020"


async def _account_service_get(
    path: str, *, params: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    try:
        async with httpx.AsyncClient(
            base_url=_account_service_base_url(),
            timeout=ACCOUNT_SERVICE_TIMEOUT,
        ) as client:
            response = await client.get(path, params=params)
            response.raise_for_status()
            if not response.content:
                return {}
            return response.json()
    except Exception as exc:
        logger.warning("Account service request {} failed: {}", path, exc)
        return None


async def _fetch_account_info_from_service(
    force_refresh: bool,
) -> tuple[Optional[Dict[str, Any]], bool]:
    payload = await _account_service_get(
        "/account/info",
        params={"force_refresh": str(force_refresh).lower()},
    )
    if not payload:
        return None, False
    if not isinstance(payload, dict):
        return None, False
    data: Optional[Dict[str, Any]] = None
    if "account_info" in payload and isinstance(payload["account_info"], dict):
        data = payload["account_info"]
    elif "data" in payload and isinstance(payload["data"], dict):
        data = payload["data"]
    elif "result" in payload and isinstance(payload["result"], dict):
        data = payload["result"]
    elif all(key in {"success", "from_cache"} for key in payload.keys()):
        data = {}
    else:
        data = {k: v for k, v in payload.items() if k not in {"success", "from_cache"}}
        if not data:
            data = None
    from_cache = bool(payload.get("from_cache", False))
    return data, from_cache


async def _fetch_positions_from_service(
    *, fast: bool, force_refresh: bool
) -> tuple[Optional[List[Any]], bool]:
    payload = await _account_service_get(
        "/account/positions",
        params={
            "fast": str(fast).lower(),
            "force_refresh": str(force_refresh).lower(),
        },
    )
    if not payload:
        return None, False
    if not isinstance(payload, dict):
        return None, False
    data = payload.get("positions")
    if data is None:
        data = payload.get("data")
    if data is None and isinstance(payload.get("result"), list):
        data = payload.get("result")
    from_cache = bool(payload.get("from_cache", False))
    if isinstance(data, list):
        return data, from_cache
    return None, from_cache


@router.post("/api/orders/backfill_commissions")
async def backfill_commissions(
    days: int = 7, user: Optional[str] = Depends(get_current_user)
):
    """Backfill commissions and PnL for recent orders."""
    if not connection_manager or not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    try:
        loop = asyncio.get_event_loop()
        updates = await asyncio.wait_for(
            loop.run_in_executor(
                None, lambda: connection_manager.backfill_commissions(days)
            ),
            timeout=30.0,
        )
        updates = int(updates or 0)
        logger.info(
            f"Backfill commissions completed: updates={updates}, days={days}"
        )
        return {"success": True, "updates": updates, "days": days}
    except asyncio.TimeoutError:
        logger.warning("Backfill commissions request timed out")
        raise HTTPException(status_code=408, detail="Backfill request timed out")
    except Exception as e:
        logger.error(f"Backfill commissions error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/account_info")
async def get_account_info(
    user: Optional[str] = Depends(get_current_user),
    force_refresh: bool = False,
):
    """Retrieve IBKR account information, preferring cached snapshots."""

    snapshot = await read_account_info_snapshot()
    is_connected = bool(connection_manager and connection_manager.is_connected())

    if snapshot and not force_refresh:
        return _snapshot_response(
            snapshot,
            data_key="account_info",
            connected=is_connected,
            from_cache=True,
            default={},
        )

    if not is_connected:
        if snapshot:
            logger.warning(
                "Not connected to IBKR, returning cached account_info snapshot",
            )
            return _snapshot_response(
                snapshot,
                data_key="account_info",
                connected=False,
                from_cache=True,
                default={},
                warning="Not connected to IBKR",
            )

        last_err = None
        try:
            last_err = connection_manager.get_last_connect_error()
        except Exception:
            last_err = None
        logger.warning("Not connected to IBKR and no cache available for account_info")
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "connected": False,
                "error": "Not connected to IBKR",
                "last_error": last_err,
            },
        )

    info, service_cache = await _fetch_account_info_from_service(force_refresh)
    if info is None:
        if snapshot:
            logger.warning(
                "Account service unavailable, falling back to cached account_info",
            )
            return _snapshot_response(
                snapshot,
                data_key="account_info",
                connected=is_connected,
                from_cache=True,
                default={},
                warning="Account service unavailable",
            )
        raise HTTPException(status_code=503, detail="Account service unavailable")

    refreshed_snapshot = await read_account_info_snapshot()
    return _snapshot_response(
        refreshed_snapshot,
        data_key="account_info",
        connected=is_connected,
        from_cache=service_cache,
        default={},
        data_override=info,
    )

@router.get("/api/positions")
async def get_positions(
    user: Optional[str] = Depends(get_current_user),
    fast: bool = True,
    use_cache: bool = True,
    force_refresh: bool = False,
):
    """Retrieve current positions, preferring Redis snapshots."""

    snapshot = await read_positions_snapshot()
    is_connected = bool(connection_manager and connection_manager.is_connected())
    force_refresh = force_refresh or not use_cache

    if snapshot and not force_refresh:
        return _snapshot_response(
            snapshot,
            data_key="positions",
            connected=is_connected,
            from_cache=True,
            default=[],
        )

    if not is_connected:
        if snapshot:
            logger.warning(
                "Not connected to IBKR, returning cached positions snapshot",
            )
            return _snapshot_response(
                snapshot,
                data_key="positions",
                connected=False,
                from_cache=True,
                default=[],
                warning="Not connected to IBKR",
            )
        logger.warning("Not connected to IBKR and no cached positions available")
        return {
            "success": True,
            "positions": [],
            "from_cache": False,
            "warning": "Not connected to IBKR",
        }

    positions, service_cache = await _fetch_positions_from_service(
        fast=fast,
        force_refresh=force_refresh,
    )
    if positions is None:
        if snapshot:
            logger.warning(
                "Account service unavailable, falling back to cached positions",
            )
            return _snapshot_response(
                snapshot,
                data_key="positions",
                connected=is_connected,
                from_cache=True,
                default=[],
                warning="Account service unavailable",
            )
        raise HTTPException(status_code=503, detail="Account service unavailable")

    refreshed_snapshot = await read_positions_snapshot()
    return _snapshot_response(
        refreshed_snapshot,
        data_key="positions",
        connected=is_connected,
        from_cache=service_cache,
        default=[],
        data_override=positions,
    )

@router.get("/api/account/daily-summary")
async def get_account_daily_summary(
    days: int = 30,
    user: Optional[str] = Depends(get_current_user),
):
    """按照美东时间统计每日账户盈余与Realized PnL"""
    if days < 1 or days > 365:
        raise HTTPException(status_code=400, detail="days must be between 1 and 365")

    try:
        ny_now = datetime.now(NY_TZ)
        end_date = ny_now.date()
        start_date = end_date - timedelta(days=days - 1)

        cache_data = await load_all_daily_totals()
        daily_stats: Dict[date, Dict[str, float]] = {}
        orders: Optional[List[Dict[str, Any]]] = None

        if cache_data and len(cache_data) > 0:
            for day_str, metrics in cache_data.items():
                try:
                    trade_day = date.fromisoformat(day_str)
                except (TypeError, ValueError):
                    continue
                if trade_day < start_date or trade_day > end_date:
                    continue
                pnl_raw = metrics.get("pnl", 0.0)
                orders_raw = metrics.get("orders", 0)
                try:
                    pnl_value = float(pnl_raw)
                except (TypeError, ValueError):
                    pnl_value = 0.0
                try:
                    orders_count = int(orders_raw)
                except (TypeError, ValueError):
                    orders_count = 0
                daily_stats[trade_day] = {"pnl": pnl_value, "orders": orders_count}
        else:
            db = get_db_manager()
            orders = db.fetch_orders_for_pnl()
            for order in orders:
                try:
                    pnl_value = float(order.get("pnl") or 0.0)
                except (TypeError, ValueError):
                    pnl_value = 0.0
                ts = parse_local_datetime(order.get("executed_at") or order.get("created_at"))
                if ts is None:
                    continue
                ts_ny = ts.astimezone(NY_TZ)
                trade_date = ts_ny.date()
                if trade_date < start_date or trade_date > end_date:
                    continue
                entry = daily_stats.setdefault(trade_date, {"pnl": 0.0, "orders": 0})
                entry["pnl"] += pnl_value
                entry["orders"] += 1
            if cache_data == {} and orders is not None:
                try:
                    await rebuild_account_daily_cache(orders)
                except Exception as exc:  # pragma: no cover - defensive guard
                    logger.debug(
                        "Failed to rebuild account daily cache",
                        error=str(exc),
                    )

        ordered_dates: List[date] = []
        current = start_date
        while current <= end_date:
            ordered_dates.append(current)
            current += timedelta(days=1)

        net_cache = await read_net_liquidation_snapshot()
        cached_net_value: Optional[float] = None
        cached_net_ts: Optional[datetime] = None
        cached_net_ts_raw: Optional[str] = None
        cached_net_source: Optional[str] = None
        if isinstance(net_cache, dict):
            try:
                cached_net_value = float(net_cache.get("value"))
            except (TypeError, ValueError):
                cached_net_value = None
            ts_raw = net_cache.get("timestamp")
            cached_net_ts_raw = ts_raw if isinstance(ts_raw, str) else None
            if isinstance(ts_raw, datetime):
                cached_net_ts = ensure_local_timezone(ts_raw)
            elif isinstance(ts_raw, str):
                cached_net_ts = parse_local_datetime(ts_raw)
            cached_net_source = net_cache.get("source")

        net_liquidation: Optional[float] = None
        net_liquidation_timestamp: Optional[datetime] = None
        net_liquidation_timestamp_raw: Optional[str] = cached_net_ts_raw
        surplus_source = "unknown"
        net_snapshot_written = False

        def resolve_net(snapshot: Optional[Dict[str, Any]]) -> Optional[float]:
            if not isinstance(snapshot, dict):
                return None
            try:
                value = snapshot.get("net_liquidation")
                if value is None:
                    value = snapshot.get("netLiquidation")
                if value is None:
                    return None
                return float(value)
            except (TypeError, ValueError):
                return None

        service_info, _ = await _fetch_account_info_from_service(False)
        if net_liquidation is None and service_info is not None:
            resolved = resolve_net(service_info)
            if resolved is not None:
                net_liquidation = resolved
                net_liquidation_timestamp = now_local()
                surplus_source = "account_service"
                await write_net_liquidation_snapshot(
                    resolved,
                    source="account_service",
                    timestamp=net_liquidation_timestamp,
                )
                net_snapshot_written = True

        account_snapshot = await read_account_info_snapshot()
        cached_snapshot = account_snapshot.get("data") if isinstance(account_snapshot, dict) else None
        cached_snapshot_ts = parse_local_datetime(account_snapshot.get("updated_at")) if isinstance(account_snapshot, dict) else None

        if net_liquidation is None and cached_net_value is not None:
            net_liquidation = cached_net_value
            surplus_source = cached_net_source or "redis"
            net_liquidation_timestamp = cached_net_ts or net_liquidation_timestamp
            if cached_net_ts_raw:
                net_liquidation_timestamp_raw = cached_net_ts_raw

        if net_liquidation is None:
            resolved_cache = resolve_net(cached_snapshot)
            if resolved_cache is not None:
                net_liquidation = resolved_cache
                surplus_source = "ibkr_cached"
                if cached_snapshot_ts is not None:
                    net_liquidation_timestamp = ensure_local_timezone(cached_snapshot_ts)
                if not net_snapshot_written:
                    await write_net_liquidation_snapshot(
                        resolved_cache,
                        source="ibkr_cached",
                        timestamp=net_liquidation_timestamp,
                    )
                    net_snapshot_written = True

        if net_liquidation is None:
            fallback_values = [
                getattr(core.risk_manager, "equity_last", None),
                getattr(core.risk_manager, "sod_equity", None),
            ]
            for candidate in fallback_values:
                if candidate is None:
                    continue
                try:
                    net_liquidation = float(candidate)
                    surplus_source = "risk_manager"
                    net_liquidation_timestamp = now_local()
                    if not net_snapshot_written:
                        await write_net_liquidation_snapshot(
                            net_liquidation,
                            source="risk_manager",
                            timestamp=net_liquidation_timestamp,
                        )
                        net_snapshot_written = True
                    break
                except (TypeError, ValueError):
                    continue

        total_pnl = sum(
            float(daily_stats.get(day, {}).get("pnl", 0.0)) for day in ordered_dates
        )
        base_surplus = None
        if net_liquidation is not None:
            base_surplus = float(net_liquidation) - float(total_pnl)

        summary: List[Dict[str, Optional[float]]] = []
        running_surplus = base_surplus
        for date_item in ordered_dates:
            stats = daily_stats.get(date_item, {"pnl": 0.0, "orders": 0})
            pnl_value = stats.get("pnl", 0.0) or 0.0
            orders_count = stats.get("orders", 0) or 0
            try:
                pnl_value_float = float(pnl_value)
            except (TypeError, ValueError):
                pnl_value_float = 0.0
            try:
                orders_count_int = int(orders_count)
            except (TypeError, ValueError):
                orders_count_int = 0

            if running_surplus is None:
                start_val = None
                end_val = None
            else:
                start_val = running_surplus
                end_val = running_surplus + pnl_value_float
                running_surplus = end_val

            summary.append(
                {
                    "date": date_item.isoformat(),
                    "orders": orders_count_int,
                    "has_orders": orders_count_int > 0,
                    "realized_pnl": round(pnl_value_float, 2),
                    "starting_surplus": round(start_val, 2) if start_val is not None else None,
                    "ending_surplus": round(end_val, 2) if end_val is not None else None,
                }
            )

        timestamp_iso = None
        if net_liquidation_timestamp is not None:
            timestamp_iso = ensure_local_timezone(net_liquidation_timestamp).isoformat()
        elif net_liquidation_timestamp_raw:
            timestamp_iso = net_liquidation_timestamp_raw

        return {
            "success": True,
            "days": summary,
            "range": {"start": start_date.isoformat(), "end": end_date.isoformat()},
            "net_liquidation": float(net_liquidation) if net_liquidation is not None else None,
            "net_liquidation_timestamp": timestamp_iso,
            "base_surplus": round(base_surplus, 2) if base_surplus is not None else None,
            "surplus_source": surplus_source,
            "timezone": "America/New_York",
        }

    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Failed to build daily account summary: {exc}")
        raise HTTPException(status_code=500, detail="Failed to load account summary")
