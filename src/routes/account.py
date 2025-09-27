import asyncio
from datetime import datetime
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any

from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import JSONResponse
from loguru import logger

from src.utils import now_local, parse_local_datetime, NY_TZ
from src.account_snapshot_cache import (
    read_account_info_snapshot,
    read_positions_snapshot,
    write_account_info_snapshot,
    write_positions_snapshot,
)

import core
from core import connection_manager
from src.database import get_db_manager
from .auth import get_current_user

router = APIRouter()


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


def _get_active_ib_client():
    """Return an initialized IBKR client if available, creating one on-demand."""
    ib_client = getattr(core, "ibkr_client", None)
    if ib_client is not None:
        return ib_client

    factory = getattr(core, "IBKRClient", None)
    if factory is None:
        logger.error("IBKRClient factory is not available on core module")
        return None

    try:
        ib_client = factory()
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.error(f"Failed to initialize IBKR client: {exc}")
        return None

    core.ibkr_client = ib_client

    # Best effort to keep risk manager dependency in sync when available.
    try:
        risk_manager = getattr(core, "risk_manager", None)
        if risk_manager is not None:
            setattr(risk_manager, "ibkr_client", ib_client)
    except Exception as dep_err:  # pragma: no cover - defensive guard
        logger.debug(f"Failed to sync risk_manager IBKR client dependency: {dep_err}")

    return ib_client


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
                "Not connected to IBKR, returning cached account_info snapshot"
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

    ib_client = _get_active_ib_client()
    if ib_client is None:
        logger.error("IBKR client unavailable while connection manager reports connected")
        raise HTTPException(status_code=503, detail="IBKR client unavailable")

    service = getattr(core, "account_snapshot_service", None)
    reason = "api_force" if force_refresh else "api_miss"

    try:
        if service:
            info = await asyncio.wait_for(
                service.refresh_account_info(reason=reason), timeout=10.0
            )
        else:
            info = await asyncio.wait_for(
                ib_client.get_account_info(), timeout=10.0
            )
            await write_account_info_snapshot(info or {}, reason)
    except asyncio.TimeoutError:
        logger.warning("Account info request timed out")
        if snapshot:
            return _snapshot_response(
                snapshot,
                data_key="account_info",
                connected=is_connected,
                from_cache=True,
                default={},
                warning="Account info request timed out, showing cached data",
            )
        return {
            "success": False,
            "connected": True,
            "error": "Account info request timed out",
        }
    except Exception as e:
        logger.error(f"Get account info error: {e}")
        if snapshot:
            return _snapshot_response(
                snapshot,
                data_key="account_info",
                connected=is_connected,
                from_cache=True,
                default={},
                warning="Account info request failed, showing cached data",
            )
        raise HTTPException(status_code=500, detail=str(e))

    refreshed_snapshot = await read_account_info_snapshot()
    return _snapshot_response(
        refreshed_snapshot,
        data_key="account_info",
        connected=is_connected,
        from_cache=False,
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
                "Not connected to IBKR, returning cached positions snapshot"
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

    ib_client = _get_active_ib_client()
    if ib_client is None:
        logger.error("IBKR client unavailable while fetching positions")
        raise HTTPException(status_code=503, detail="IBKR client unavailable")

    service = getattr(core, "account_snapshot_service", None)
    reason = "api_force" if force_refresh else "api_miss"

    try:
        if service:
            positions = await asyncio.wait_for(
                service.refresh_positions(reason=reason, fast=fast),
                timeout=10.0,
            )
        else:
            positions = await asyncio.wait_for(
                ib_client.get_positions(fast=fast), timeout=10.0
            )
            await write_positions_snapshot(positions or [], reason)
    except asyncio.TimeoutError:
        logger.warning("Positions request timed out")
        if snapshot:
            return _snapshot_response(
                snapshot,
                data_key="positions",
                connected=is_connected,
                from_cache=True,
                default=[],
                warning="Positions request timed out, showing cached data",
            )
        return {"success": False, "error": "Positions request timed out"}
    except Exception as e:
        logger.error(f"Get positions error: {e}")
        if snapshot:
            return _snapshot_response(
                snapshot,
                data_key="positions",
                connected=is_connected,
                from_cache=True,
                default=[],
                warning="Positions request failed, showing cached data",
            )
        raise HTTPException(status_code=500, detail=str(e))

    refreshed_snapshot = await read_positions_snapshot()
    return _snapshot_response(
        refreshed_snapshot,
        data_key="positions",
        connected=is_connected,
        from_cache=False,
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

        db = get_db_manager()
        orders = db.fetch_orders_for_pnl()

        daily_stats: Dict[datetime.date, Dict[str, float]] = defaultdict(lambda: {"pnl": 0.0, "orders": 0})

        for order in orders:
            pnl_value = float(order.get('pnl') or 0.0)
            ts = parse_local_datetime(order.get('executed_at') or order.get('created_at'))
            if ts is None:
                continue
            ts_ny = ts.astimezone(NY_TZ)
            trade_date = ts_ny.date()
            if trade_date < start_date or trade_date > end_date:
                continue
            entry = daily_stats[trade_date]
            entry['pnl'] += pnl_value
            entry['orders'] += 1

        ordered_dates: List[datetime.date] = []
        current = start_date
        while current <= end_date:
            ordered_dates.append(current)
            current += timedelta(days=1)

        # Determine reference net liquidation
        net_liquidation = None
        net_liquidation_timestamp = None
        surplus_source = 'unknown'

        ib_client = getattr(core, 'ibkr_client', None)
        cached_snapshot = {}
        cached_snapshot_ts = None
        if ib_client is not None:
            cached_snapshot = getattr(ib_client, '_cached_account_info', {}) or {}
            cached_snapshot_ts = getattr(ib_client, '_cached_account_info_timestamp', None)

        def resolve_net(snapshot: Optional[Dict[str, float]]) -> Optional[float]:
            if not isinstance(snapshot, dict):
                return None
            try:
                value = snapshot.get('net_liquidation')
                if value is None:
                    return None
                return float(value)
            except (TypeError, ValueError):
                return None

        if connection_manager.is_connected() and ib_client is not None:
            try:
                account_info = await asyncio.wait_for(ib_client.get_account_info(), timeout=5.0)
                resolved = resolve_net(account_info)
                if resolved is not None:
                    net_liquidation = resolved
                    net_liquidation_timestamp = now_local()
                    surplus_source = 'ibkr'
            except Exception as err:  # pragma: no cover - defensive logging
                logger.debug(f"Failed to fetch net liquidation from IBKR: {err}")

        if net_liquidation is None:
            resolved_cache = resolve_net(cached_snapshot)
            if resolved_cache is not None:
                net_liquidation = resolved_cache
                surplus_source = 'ibkr_cached'
                if cached_snapshot_ts:
                    net_liquidation_timestamp = cached_snapshot_ts

        if net_liquidation is None:
            fallback_values = [
                getattr(core.risk_manager, 'equity_last', None),
                getattr(core.risk_manager, 'sod_equity', None),
            ]
            for candidate in fallback_values:
                if candidate is not None:
                    try:
                        net_liquidation = float(candidate)
                        surplus_source = 'risk_manager'
                        break
                    except (TypeError, ValueError):
                        continue

        total_pnl = sum(daily_stats.get(day, {}).get('pnl', 0.0) for day in ordered_dates)
        base_surplus = None
        if net_liquidation is not None:
            base_surplus = float(net_liquidation) - float(total_pnl)

        summary: List[Dict[str, Optional[float]]] = []
        running_surplus = base_surplus
        for date_item in ordered_dates:
            stats = daily_stats.get(date_item, {"pnl": 0.0, "orders": 0})
            pnl_value = float(stats.get('pnl', 0.0) or 0.0)
            orders_count = int(stats.get('orders', 0) or 0)

            if running_surplus is None:
                start_val = None
                end_val = None
            else:
                start_val = running_surplus
                end_val = running_surplus + pnl_value
                running_surplus = end_val

            summary.append(
                {
                    "date": date_item.isoformat(),
                    "orders": orders_count,
                    "has_orders": orders_count > 0,
                    "realized_pnl": round(pnl_value, 2),
                    "starting_surplus": round(start_val, 2) if start_val is not None else None,
                    "ending_surplus": round(end_val, 2) if end_val is not None else None,
                }
            )

        return {
            "success": True,
            "days": summary,
            "range": {"start": start_date.isoformat(), "end": end_date.isoformat()},
            "net_liquidation": float(net_liquidation) if net_liquidation is not None else None,
            "net_liquidation_timestamp": net_liquidation_timestamp.isoformat() if net_liquidation_timestamp else None,
            "base_surplus": round(base_surplus, 2) if base_surplus is not None else None,
            "surplus_source": surplus_source,
            "timezone": "America/New_York",
        }

    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Failed to build daily account summary: {exc}")
        raise HTTPException(status_code=500, detail="Failed to load account summary")
