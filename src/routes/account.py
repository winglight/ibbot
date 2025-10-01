import asyncio
import os
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel, Field

from ibbot.redis import (
    enqueue_ib_command,
    ensure_redis,
    request_ib_command,
    wait_for_command_result,
)
from src.account_snapshot_cache import (
    read_account_info_snapshot,
    read_account_service_health,
    read_positions_snapshot,
)
from src.account_snapshot_service import account_snapshot_service
from src.utils import NY_TZ, ensure_local_timezone, now_local, parse_local_datetime
from src.utils.account_pnl_cache import (
    load_all_daily_totals,
    read_net_liquidation_snapshot,
    rebuild_account_daily_cache,
    write_net_liquidation_snapshot,
)

import core
from core import get_core_container
from .auth import get_current_user

router = APIRouter()
core_container = get_core_container()
connection_manager = core_container.connection_manager


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
    service_health: Optional[Dict[str, Any]] = None,
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
    if service_health is not None:
        response["service_health"] = dict(service_health)

    return response


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


ACCOUNT_SERVICE_BASE_URL = os.getenv("ACCOUNT_SNAPSHOT_API_URL") or os.getenv(
    "ACCOUNT_SERVICE_API_URL"
)
ACCOUNT_REFRESH_SOURCE = os.getenv("ACCOUNT_REFRESH_SOURCE", "ui-gateway")
ACCOUNT_HEALTH_STALE_SECONDS = _float_env("ACCOUNT_HEALTH_STALE_SECONDS", 180.0)
COMMAND_TIMEOUT_SECONDS = float(account_snapshot_service.config.command_timeout_seconds)
COMMAND_RESULT_TTL = int(account_snapshot_service.config.command_result_ttl_seconds)
COMMAND_QUEUE_KEY = account_snapshot_service.config.command_queue_key
COMMAND_RESULT_PREFIX = account_snapshot_service.config.command_result_prefix
ACCOUNT_COMMAND_TIMEOUT_BUFFER = _float_env("ACCOUNT_COMMAND_TIMEOUT_BUFFER", 5.0)
ACCOUNT_COMMAND_WAIT_TIMEOUT = COMMAND_TIMEOUT_SECONDS + ACCOUNT_COMMAND_TIMEOUT_BUFFER
ACCOUNT_API_TIMEOUT = COMMAND_TIMEOUT_SECONDS + ACCOUNT_COMMAND_TIMEOUT_BUFFER


def _evaluate_service_health(
    health: Optional[Dict[str, Any]]
) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
    if not health:
        return False, None, None

    payload = dict(health)
    last_error_raw = payload.get("last_error")
    last_error = str(last_error_raw).strip() if last_error_raw else None
    if last_error == "":
        last_error = None

    timestamps: List[datetime] = []
    for field in ("last_seen", "last_success", "updated_at"):
        raw = payload.get(field)
        candidate: Optional[datetime] = None
        if isinstance(raw, datetime):
            candidate = ensure_local_timezone(raw)
        elif isinstance(raw, str):
            candidate = parse_local_datetime(raw)
        if candidate is not None:
            try:
                timestamps.append(ensure_local_timezone(candidate))
            except Exception:
                timestamps.append(candidate)

    age_seconds: Optional[float] = None
    if timestamps:
        try:
            newest = max(timestamps)
            age_seconds = max(0.0, (now_local() - newest).total_seconds())
        except Exception:
            age_seconds = None

    status = str(payload.get("status") or "").lower()
    connected = False

    if status in {"ok", "healthy", "ready", "connected", "online"} and not last_error:
        connected = True
    if not status and age_seconds is not None and age_seconds <= ACCOUNT_HEALTH_STALE_SECONDS:
        connected = not last_error
    if status in {"error", "failed", "offline"}:
        connected = False

    if age_seconds is not None:
        payload["age_seconds"] = age_seconds
        if age_seconds > ACCOUNT_HEALTH_STALE_SECONDS:
            connected = False

    if last_error:
        payload["last_error"] = last_error
        connected = False

    return connected, payload, last_error


async def _request_snapshot_refresh(
    command: str,
    payload: Dict[str, Any],
    *,
    timeout: Optional[float] = None,
) -> Tuple[Optional[Any], Optional[str]]:
    redis = await ensure_redis(url=account_snapshot_service.config.redis_url)
    if redis is None:
        return None, "Redis unavailable"

    try:
        result = await request_ib_command(
            redis,
            command,
            payload,
            queue_key=COMMAND_QUEUE_KEY,
            result_prefix=COMMAND_RESULT_PREFIX,
            timeout=float(timeout or COMMAND_TIMEOUT_SECONDS),
            result_ttl=COMMAND_RESULT_TTL,
            source=ACCOUNT_REFRESH_SOURCE,
        )
    except Exception as exc:
        logger.warning(
            "Failed to dispatch account snapshot command",
            command=command,
            error=str(exc),
        )
        return None, str(exc)

    if not result:
        return None, "Command timeout"

    status = str(result.get("status") or "ok").lower()
    if status in {"error", "failed"}:
        return None, result.get("error") or "Command failed"

    payload_value = result.get("data") or result.get("result") or result
    return payload_value, None


async def _http_refresh(
    path: str,
    payload: Dict[str, Any],
    *,
    data_field: str,
) -> Tuple[Optional[Any], Optional[str]]:
    base_url = ACCOUNT_SERVICE_BASE_URL
    if not base_url:
        return None, None

    url = f"{base_url.rstrip('/')}{path}"

    try:
        async with httpx.AsyncClient(timeout=ACCOUNT_API_TIMEOUT) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Account service HTTP error",
            url=url,
            status_code=exc.response.status_code,
        )
        return None, f"HTTP {exc.response.status_code}"
    except httpx.RequestError as exc:
        logger.warning(
            "Account service request failed",
            url=url,
            error=str(exc),
        )
        return None, str(exc)

    try:
        data = response.json()
    except ValueError:
        return None, "Invalid response"

    if not isinstance(data, dict):
        return None, "Invalid response"

    if data.get("success") is False and data.get("error"):
        return None, str(data.get("error"))

    payload_value = data.get(data_field)
    if payload_value is None:
        payload_value = data.get("data") or data.get("result")

    return payload_value, None


async def _http_refresh_account_info(reason: str) -> Tuple[Optional[Any], Optional[str]]:
    return await _http_refresh(
        "/refresh/account-info",
        {"reason": reason},
        data_field="account_info",
    )


async def _http_refresh_positions(
    reason: str, *, fast: bool
) -> Tuple[Optional[Any], Optional[str]]:
    return await _http_refresh(
        "/refresh/positions",
        {"reason": reason, "fast_positions": fast},
        data_field="positions",
    )


class RefreshTarget(str, Enum):
    ACCOUNT_INFO = "account_info"
    POSITIONS = "positions"


class AccountRefreshRequest(BaseModel):
    target: RefreshTarget = Field(default=RefreshTarget.ACCOUNT_INFO)
    reason: Optional[str] = Field(default=None, max_length=64)
    fast: Optional[bool] = None
    wait: bool = True
    timeout: Optional[float] = Field(default=None, ge=1.0, le=600.0)


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
    raw_health = await read_account_service_health()
    connected, service_health, health_error = _evaluate_service_health(raw_health)

    if snapshot and not force_refresh:
        return _snapshot_response(
            snapshot,
            data_key="account_info",
            connected=connected,
            from_cache=True,
            default={},
            service_health=service_health,
        )

    reason = "api_force" if force_refresh else "api_miss"
    data_override: Optional[Any] = None
    refresh_error: Optional[str] = None

    if force_refresh or not snapshot:
        data_override, refresh_error = await _request_snapshot_refresh(
            "get_account_info",
            {"reason": reason},
            timeout=ACCOUNT_COMMAND_WAIT_TIMEOUT,
        )

        if data_override is None:
            http_data, http_error = await _http_refresh_account_info(reason)
            if http_data is not None:
                data_override = http_data
                refresh_error = None
            elif refresh_error is None and http_error:
                refresh_error = http_error

    if snapshot:
        warning: Optional[str] = None
        if refresh_error:
            warning = f"Account info refresh failed: {refresh_error}"
        elif not connected:
            warning = "Account snapshot service offline"

        if data_override is not None:
            refreshed_snapshot = await read_account_info_snapshot() or snapshot
            return _snapshot_response(
                refreshed_snapshot,
                data_key="account_info",
                connected=connected,
                from_cache=False,
                default={},
                data_override=data_override,
                warning=warning,
                service_health=service_health,
            )

        return _snapshot_response(
            snapshot,
            data_key="account_info",
            connected=connected,
            from_cache=True,
            default={},
            warning=warning,
            service_health=service_health,
        )

    if data_override is not None:
        return _snapshot_response(
            None,
            data_key="account_info",
            connected=connected,
            from_cache=False,
            default={},
            data_override=data_override,
            warning=None if connected else "Account snapshot service offline",
            service_health=service_health,
        )

    error_message = refresh_error or health_error or "Account snapshot data unavailable"
    status_code = 503 if not connected else 500
    logger.warning(
        "Account info unavailable",
        error=error_message,
        connected=connected,
    )

    content: Dict[str, Any] = {
        "success": False,
        "connected": connected,
        "error": error_message,
    }
    if service_health is not None:
        content["service_health"] = service_health
    if health_error:
        content["last_error"] = health_error

    return JSONResponse(status_code=status_code, content=content)


@router.get("/api/positions")
async def get_positions(
    user: Optional[str] = Depends(get_current_user),
    fast: bool = True,
    use_cache: bool = True,
    force_refresh: bool = False,
):
    """Retrieve current positions, preferring Redis snapshots."""

    snapshot = await read_positions_snapshot()
    raw_health = await read_account_service_health()
    connected, service_health, health_error = _evaluate_service_health(raw_health)
    force_refresh = force_refresh or not use_cache

    if snapshot and not force_refresh:
        return _snapshot_response(
            snapshot,
            data_key="positions",
            connected=connected,
            from_cache=True,
            default=[],
            service_health=service_health,
        )

    reason = "api_force" if force_refresh else "api_miss"
    data_override: Optional[Any] = None
    refresh_error: Optional[str] = None

    if force_refresh or not snapshot:
        data_override, refresh_error = await _request_snapshot_refresh(
            "get_positions",
            {"reason": reason, "fast": fast},
            timeout=ACCOUNT_COMMAND_WAIT_TIMEOUT,
        )

        if data_override is None:
            http_data, http_error = await _http_refresh_positions(reason, fast=fast)
            if http_data is not None:
                data_override = http_data
                refresh_error = None
            elif refresh_error is None and http_error:
                refresh_error = http_error

    if snapshot:
        warning: Optional[str] = None
        if refresh_error:
            warning = f"Positions refresh failed: {refresh_error}"
        elif not connected:
            warning = "Account snapshot service offline"

        if data_override is not None:
            refreshed_snapshot = await read_positions_snapshot() or snapshot
            return _snapshot_response(
                refreshed_snapshot,
                data_key="positions",
                connected=connected,
                from_cache=False,
                default=[],
                data_override=data_override,
                warning=warning,
                service_health=service_health,
            )

        return _snapshot_response(
            snapshot,
            data_key="positions",
            connected=connected,
            from_cache=True,
            default=[],
            warning=warning,
            service_health=service_health,
        )

    if data_override is not None:
        return _snapshot_response(
            None,
            data_key="positions",
            connected=connected,
            from_cache=False,
            default=[],
            data_override=data_override,
            warning=None if connected else "Account snapshot service offline",
            service_health=service_health,
        )

    error_message = refresh_error or health_error or "Positions snapshot unavailable"
    status_code = 503 if not connected else 500
    logger.warning(
        "Positions unavailable",
        error=error_message,
        connected=connected,
    )

    content: Dict[str, Any] = {
        "success": False,
        "positions": [],
        "error": error_message,
        "connected": connected,
    }
    if service_health is not None:
        content["service_health"] = service_health
    if health_error:
        content["last_error"] = health_error

    return JSONResponse(status_code=status_code, content=content)


@router.post("/api/account/refresh")
async def refresh_account_snapshots(
    data: AccountRefreshRequest, user: Optional[str] = Depends(get_current_user)
) -> Dict[str, Any]:
    """Enqueue a refresh command for account data and optionally wait for the result."""

    redis = await ensure_redis(url=account_snapshot_service.config.redis_url)
    if redis is None:
        raise HTTPException(status_code=503, detail="Redis unavailable")

    reason = data.reason or "api_force"
    target = data.target

    if target is RefreshTarget.ACCOUNT_INFO:
        command = "get_account_info"
        payload: Dict[str, Any] = {"reason": reason}
    else:
        command = "get_positions"
        payload = {
            "reason": reason,
            "fast": True if data.fast is None else bool(data.fast),
        }

    command_id = await enqueue_ib_command(
        redis,
        command,
        payload,
        queue_key=COMMAND_QUEUE_KEY,
        result_prefix=COMMAND_RESULT_PREFIX,
        result_ttl=COMMAND_RESULT_TTL,
        source=ACCOUNT_REFRESH_SOURCE,
    )

    if not command_id:
        raise HTTPException(status_code=503, detail="Failed to enqueue refresh command")

    response: Dict[str, Any] = {
        "success": True,
        "command_id": command_id,
        "status": "queued",
        "target": target.value,
        "reason": reason,
    }

    if data.wait:
        timeout = float(data.timeout) if data.timeout is not None else ACCOUNT_COMMAND_WAIT_TIMEOUT
        result = await wait_for_command_result(
            redis,
            command_id,
            result_prefix=COMMAND_RESULT_PREFIX,
            timeout=timeout,
        )
        response["waited"] = True
        response["timeout"] = timeout
        if result is None:
            response["success"] = False
            response["status"] = "timeout"
            response["error"] = "Command result not available"
        else:
            status = str(result.get("status") or "ok").lower()
            response["status"] = status
            response["result"] = result
            if status in {"error", "failed"}:
                response["success"] = False
                if result.get("error"):
                    response["error"] = result.get("error")
            else:
                response["success"] = True
    else:
        response["waited"] = False
        response["timeout"] = (
            float(data.timeout)
            if data.timeout is not None
            else ACCOUNT_COMMAND_WAIT_TIMEOUT
        )

    return response


@router.get("/api/account/daily-summary")
async def get_account_daily_summary(
    days: int = 30,
    user: Optional[str] = Depends(get_current_user),
):
    """按照美东时间统计每日账户盈余与Realized PnL"""
    if days < 1 or days > 365:
        raise HTTPException(status_code=400, detail="days must be between 1 and 365")

    try:
        service_connected = False
        service_health: Optional[Dict[str, Any]] = None

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
            db = core_container.get_db_manager()
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

        service_health_raw = await read_account_service_health()
        service_connected, service_health, _ = _evaluate_service_health(
            service_health_raw
        )

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

        account_snapshot = await read_account_info_snapshot()

        net_liquidation: Optional[float] = None
        net_liquidation_timestamp: Optional[datetime] = None
        net_liquidation_timestamp_raw: Optional[str] = cached_net_ts_raw
        surplus_source = "unknown"
        net_snapshot_written = False

        def resolve_net(snapshot: Optional[Dict[str, Any]]) -> Optional[float]:
            if not isinstance(snapshot, dict):
                return None
            containers = [snapshot]
            data = snapshot.get("data") if isinstance(snapshot.get("data"), dict) else None
            if data:
                containers.append(data)
            for container in containers:
                if not isinstance(container, dict):
                    continue
                for key in (
                    "net_liquidation",
                    "netLiquidation",
                    "net_liquidation_value",
                ):
                    if key not in container:
                        continue
                    try:
                        return float(container[key])
                    except (TypeError, ValueError):
                        continue
            return None

        def resolve_snapshot_timestamp(
            snapshot: Optional[Dict[str, Any]]
        ) -> Tuple[Optional[datetime], Optional[str]]:
            if not isinstance(snapshot, dict):
                return None, None
            containers = [snapshot]
            data = snapshot.get("data") if isinstance(snapshot.get("data"), dict) else None
            if data:
                containers.append(data)
            for container in containers:
                if not isinstance(container, dict):
                    continue
                for key in ("timestamp", "updated_at", "received_at", "refreshed_at"):
                    raw = container.get(key)
                    if isinstance(raw, datetime):
                        return ensure_local_timezone(raw), raw.isoformat()
                    if isinstance(raw, str):
                        parsed = parse_local_datetime(raw)
                        if parsed is not None:
                            parsed = ensure_local_timezone(parsed)
                            return parsed, raw
            return None, None

        if cached_net_value is not None:
            net_liquidation = cached_net_value
            surplus_source = cached_net_source or "redis"
            net_liquidation_timestamp = cached_net_ts or net_liquidation_timestamp
            if cached_net_ts_raw:
                net_liquidation_timestamp_raw = cached_net_ts_raw

        if net_liquidation is None:
            resolved_snapshot = resolve_net(account_snapshot)
            if resolved_snapshot is not None:
                net_liquidation = resolved_snapshot
                surplus_source = "account_snapshot"
                ts_obj, ts_raw = resolve_snapshot_timestamp(account_snapshot)
                if ts_obj is not None:
                    net_liquidation_timestamp = ts_obj
                if ts_raw:
                    net_liquidation_timestamp_raw = ts_raw
                if not net_snapshot_written:
                    await write_net_liquidation_snapshot(
                        net_liquidation,
                        source="account_snapshot",
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
            "service_health": service_health,
            "service_connected": service_connected,
        }

    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Failed to build daily account summary: {exc}")
        raise HTTPException(status_code=500, detail="Failed to load account summary")
