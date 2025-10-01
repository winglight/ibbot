from datetime import date, datetime, timedelta
from typing import Optional, Dict, List, Any, Mapping

import os
import uuid

from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import JSONResponse
from loguru import logger

from src.utils import now_local, parse_local_datetime, NY_TZ, ensure_local_timezone
from src.account_snapshot_cache import (
    read_account_info_snapshot,
    read_positions_snapshot,
)
from src.utils.account_pnl_cache import (
    load_all_daily_totals,
    read_net_liquidation_snapshot,
    write_net_liquidation_snapshot,
)

import core
from src.ibbot.redis import DEFAULT_COMMAND_QUEUE, enqueue_ib_command
from src.routes.service_clients import (
    AccountServiceClient,
    ServiceClientError,
    ServiceResponseError,
    ServiceUnavailableError,
)
from .auth import get_current_user

router = APIRouter()

ACCOUNT_SERVICE_URL = os.getenv("ACCOUNT_SERVICE_URL", "http://localhost:8020").rstrip("/")
ACCOUNT_SERVICE_TIMEOUT = float(os.getenv("ACCOUNT_SERVICE_TIMEOUT", "10.0"))

_ACCOUNT_SERVICE_CLIENT = AccountServiceClient()

_COMMAND_QUEUE_OVERRIDE = os.getenv("IB_COMMAND_QUEUE_KEY", "").strip() or None


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


def _get_account_service_client() -> AccountServiceClient:
    return _ACCOUNT_SERVICE_CLIENT


def _extract_connection_flag(payload: Any) -> Optional[bool]:
    if isinstance(payload, Mapping):
        for key in (
            "connected",
            "is_connected",
            "ibkr_connected",
            "connection",
            "connected_state",
        ):
            if key in payload:
                value = payload.get(key)
                if isinstance(value, bool):
                    return value
                if value is None:
                    continue
                if isinstance(value, (int, float)):
                    return bool(value)
                if isinstance(value, str):
                    lowered = value.strip().lower()
                    if lowered in {"true", "1", "yes", "connected", "online"}:
                        return True
                    if lowered in {"false", "0", "no", "disconnected", "offline"}:
                        return False
        for nested_key in (
            "account_info",
            "data",
            "result",
            "account_summary",
            "accountSummary",
            "summary",
        ):
            nested = payload.get(nested_key)
            if nested is not None and nested is not payload:
                nested_flag = _extract_connection_flag(nested)
                if nested_flag is not None:
                    return nested_flag
    return None


def _determine_connection_state(*payloads: Any) -> bool:
    for payload in payloads:
        flag = _extract_connection_flag(payload)
        if flag is not None:
            return flag
    for payload in payloads:
        if payload is not None:
            return True
    return False


async def _resolve_command_queue_key() -> str:
    if _COMMAND_QUEUE_OVERRIDE:
        return _COMMAND_QUEUE_OVERRIDE

    container = core.get_container()
    provider = getattr(container, "config_provider", None) if container else None
    if provider is None:
        return DEFAULT_COMMAND_QUEUE

    try:
        queue_key = await provider.get(
            "ib-command",
            "queue_key",
            default=DEFAULT_COMMAND_QUEUE,
        )
    except Exception:  # pragma: no cover - config lookup failure
        return DEFAULT_COMMAND_QUEUE

    queue_key_str = str(queue_key or "").strip()
    return queue_key_str or DEFAULT_COMMAND_QUEUE


def _service_unavailable_response(
    snapshot: Optional[Dict[str, Any]],
    *,
    data_key: str,
    default: Any,
    message: str,
) -> JSONResponse:
    if not snapshot:
        raise HTTPException(status_code=503, detail=message)

    connected = _determine_connection_state(snapshot.get("data"), snapshot)
    payload = _snapshot_response(
        snapshot,
        data_key=data_key,
        connected=connected,
        from_cache=True,
        default=default,
        warning=message,
    )
    payload["success"] = False
    return JSONResponse(
        status_code=503,
        content={
            "detail": message,
            "snapshot": payload,
        },
    )


@router.post("/api/orders/backfill_commissions")
async def backfill_commissions(
    days: int = 7, user: Optional[str] = Depends(get_current_user)
):
    """Queue a commission backfill command for asynchronous execution."""

    command_id = uuid.uuid4().hex
    payload = {
        "command_id": command_id,
        "method": "backfill_commissions",
        "args": [],
        "kwargs": {"days": days},
        "metadata": {
            "source": "api.backfill_commissions",
            "submitted_by": user or "anonymous",
        },
    }

    try:
        queue_key = await _resolve_command_queue_key()
        await enqueue_ib_command(payload, queue_key=queue_key)
    except Exception as exc:
        logger.error("Failed to enqueue backfill commissions command", exc_info=True)
        raise HTTPException(
            status_code=503, detail="Failed to enqueue backfill commissions command"
        ) from exc

    logger.info(
        "Enqueued backfill_commissions command {} for {} days",
        command_id,
        days,
    )
    return {"success": True, "command_id": command_id, "days": days}


@router.get("/api/account_info")
async def get_account_info(
    user: Optional[str] = Depends(get_current_user),
    force_refresh: bool = False,
):
    """Retrieve IBKR account information, preferring cached snapshots."""

    snapshot = await read_account_info_snapshot()
    snapshot_data = snapshot.get("data") if isinstance(snapshot, dict) else None
    connected_snapshot = _determine_connection_state(snapshot_data, snapshot)

    if snapshot and not force_refresh:
        return _snapshot_response(
            snapshot,
            data_key="account_info",
            connected=connected_snapshot,
            from_cache=True,
            default={},
        )

    client = _get_account_service_client()
    try:
        info = await client.fetch_info(force_refresh=force_refresh)
    except ServiceUnavailableError as exc:
        logger.warning("Account service unavailable for account_info: {}", exc)
        return _service_unavailable_response(
            snapshot,
            data_key="account_info",
            default={},
            message="Account service unavailable",
        )
    except ServiceResponseError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc.detail)) from exc
    except ServiceClientError as exc:
        logger.error("Account service error while fetching account info", exc_info=True)
        return _service_unavailable_response(
            snapshot,
            data_key="account_info",
            default={},
            message="Account service unavailable",
        )

    if info is None:
        logger.warning("Account service returned no account_info payload")
        return _service_unavailable_response(
            snapshot,
            data_key="account_info",
            default={},
            message="Account service unavailable",
        )

    refreshed_snapshot = await read_account_info_snapshot()
    refreshed_data = (
        refreshed_snapshot.get("data") if isinstance(refreshed_snapshot, dict) else None
    )
    connected = _determine_connection_state(info, refreshed_data, refreshed_snapshot)

    return _snapshot_response(
        refreshed_snapshot,
        data_key="account_info",
        connected=connected,
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
    snapshot_data = snapshot.get("data") if isinstance(snapshot, dict) else None
    connected_snapshot = _determine_connection_state(snapshot_data, snapshot)
    force_refresh = force_refresh or not use_cache

    if snapshot and not force_refresh:
        return _snapshot_response(
            snapshot,
            data_key="positions",
            connected=connected_snapshot,
            from_cache=True,
            default=[],
        )

    client = _get_account_service_client()
    try:
        positions = await client.fetch_positions(
            fast=fast,
            force_refresh=force_refresh,
        )
    except ServiceUnavailableError as exc:
        logger.warning("Account service unavailable for positions: {}", exc)
        return _service_unavailable_response(
            snapshot,
            data_key="positions",
            default=[],
            message="Account service unavailable",
        )
    except ServiceResponseError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc.detail)) from exc
    except ServiceClientError:
        logger.error("Account service error while fetching positions", exc_info=True)
        return _service_unavailable_response(
            snapshot,
            data_key="positions",
            default=[],
            message="Account service unavailable",
        )

    if positions is None:
        logger.warning("Account service returned no positions payload")
        return _service_unavailable_response(
            snapshot,
            data_key="positions",
            default=[],
            message="Account service unavailable",
        )

    refreshed_snapshot = await read_positions_snapshot()
    refreshed_data = (
        refreshed_snapshot.get("data") if isinstance(refreshed_snapshot, dict) else None
    )
    connected = _determine_connection_state(refreshed_data, refreshed_snapshot, positions)

    return _snapshot_response(
        refreshed_snapshot,
        data_key="positions",
        connected=connected,
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
            }

        daily_stats: Dict[date, Dict[str, float]] = {}
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

        service_info: Optional[Dict[str, Any]] = None
        try:
            service_info = await _get_account_service_client().fetch_info(
                force_refresh=False
            )
        except (ServiceUnavailableError, ServiceResponseError, ServiceClientError) as exc:
            logger.debug("Account service fetch for daily summary failed: {}", exc)
            service_info = None

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
