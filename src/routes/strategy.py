import asyncio
import inspect
from dataclasses import asdict
from typing import Optional, Dict, Any, Tuple
from datetime import datetime, timedelta
from time import monotonic

from fastapi import APIRouter, HTTPException, Depends, Request
from loguru import logger

from src.utils import now_local, parse_local_datetime
from src.utils.account_pnl_cache import rebuild_account_daily_cache_from_db
from src.utils.strategy_cache import (
    compute_performance_metrics as cache_compute_performance,
    get_daily_pnl as cache_get_daily_pnl,
    get_strategy_stats as cache_get_strategy_stats,
    rebuild_strategy_cache as cache_rebuild_strategy_cache,
)

from core import strategy_manager, queue_manager, order_manager
from .auth import get_current_user
from src.database import get_db_manager
from src.models import (
    StrategySettingsRequest,
    StrategyRequest,
    ExternalStrategyOrderRequest,
    StrategyRenameRequest,
    StrategyDataPushRequest,
    StrategyRiskSettings,
    StrategyParameterUpdateRequest,
)

router = APIRouter()

EXECUTIONS_CACHE_TTL = 2.0
DAILY_PNL_CACHE_TTL = 30.0
EXECUTIONS_FETCH_TIMEOUT = 3.0
DAILY_PNL_FETCH_TIMEOUT = 3.5
PERFORMANCE_CACHE_TTL = 30.0
PERFORMANCE_FETCH_TIMEOUT = 4.0

_executions_cache: Dict[Tuple[str, int], Dict[str, Any]] = {}
_executions_cache_lock: Optional[asyncio.Lock] = None
_executions_fetch_tasks: Dict[Tuple[str, int], asyncio.Task] = {}

_daily_pnl_cache: Dict[str, Dict[str, Any]] = {}
_daily_pnl_cache_lock: Optional[asyncio.Lock] = None
_daily_pnl_fetch_tasks: Dict[str, asyncio.Task] = {}


def _make_performance_cache_key(
    name: str,
    range_value: str,
    start_date: Optional[datetime],
    end_date: Optional[datetime],
) -> str:
    if range_value == "custom":
        start_token = start_date.isoformat() if isinstance(start_date, datetime) else ""
        end_token = end_date.isoformat() if isinstance(end_date, datetime) else ""
    else:
        start_token = ""
        end_token = ""
    return f"{name}::{range_value}::{start_token}::{end_token}"


async def _ensure_strategy_cache_populated(name: str) -> Dict[str, Any]:
    """Ensure Redis-backed caches for ``name`` exist before expensive queries."""

    stats = await cache_get_strategy_stats(name)
    if stats:
        return stats

    lock = _strategy_cache_rebuild_locks.setdefault(name, asyncio.Lock())
    async with lock:
        stats = await cache_get_strategy_stats(name)
        if stats:
            return stats

        db = get_db_manager()
        try:
            orders = await asyncio.to_thread(
                db.load_strategy_orders,
                name,
                "strategy",
                None,
            )
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.warning(
                "Failed to load strategy orders for cache rebuild",
                strategy=name,
                error=str(exc),
            )
            orders = []

        try:
            await cache_rebuild_strategy_cache(name, orders)
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.warning(
                "Strategy cache rebuild failed",
                strategy=name,
                error=str(exc),
            )

        return await cache_get_strategy_stats(name)


async def _load_strategy_performance(
    name: str,
    cache_key: str,
    *,
    range_value: str,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
) -> Dict[str, Any]:
    await _ensure_strategy_cache_populated(name)

    try:
        metrics = await cache_compute_performance(
            name, start_date=start_date, end_date=end_date
        )
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning(
            "Cached strategy performance computation failed",
            strategy=name,
            error=str(exc),
        )
        metrics = {}

    if not metrics:
        try:
            metrics = await asyncio.to_thread(
                strategy_manager.get_strategy_performance,
                name,
                start_date,
                end_date,
            )
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.error(
                "Fallback strategy performance computation failed",
                strategy=name,
                error=str(exc),
            )
            metrics = {}

    payload: Dict[str, Any] = {
        "success": True,
        "metrics": metrics or {},
        "range": range_value,
    }

    if _performance_cache_lock is None:
        raise RuntimeError("Performance cache lock unavailable")

    async with _performance_cache_lock:
        _performance_cache[cache_key] = {
            "timestamp": monotonic(),
            "data": dict(payload),
        }

    payload["from_cache"] = False
    payload["cache_age"] = 0.0
    return payload

_performance_cache: Dict[str, Dict[str, Any]] = {}
_performance_cache_lock: Optional[asyncio.Lock] = None
_performance_fetch_tasks: Dict[str, asyncio.Task] = {}

_strategy_cache_rebuild_locks: Dict[str, asyncio.Lock] = {}


async def _load_strategy_executions(
    name: str,
    limit: int,
    cache_key: Tuple[str, int],
) -> Dict[str, Any]:
    executions = await asyncio.to_thread(strategy_manager.get_executions, name, limit)
    formatted = [
        asdict(exec_item) if hasattr(exec_item, "__dict__") else exec_item
        for exec_item in executions
    ]
    payload: Dict[str, Any] = {"success": True, "executions": formatted}

    if _executions_cache_lock is None:
        raise RuntimeError("Executions cache lock unavailable")

    async with _executions_cache_lock:
        _executions_cache[cache_key] = {
            "timestamp": monotonic(),
            "data": dict(payload),
        }

    payload["from_cache"] = False
    payload["cache_age"] = 0.0
    return payload


async def _load_strategy_daily_pnl(name: str, cache_key: str) -> Dict[str, Any]:
    stats = await _ensure_strategy_cache_populated(name)
    rows = await cache_get_daily_pnl(name)
    if not rows:
        rows = []
        if stats:
            logger.debug("Strategy daily PnL cache empty", strategy=name)
    payload: Dict[str, Any] = {"success": True, "pnl": rows}

    if _daily_pnl_cache_lock is None:
        raise RuntimeError("Daily PnL cache lock unavailable")

    async with _daily_pnl_cache_lock:
        _daily_pnl_cache[cache_key] = {
            "timestamp": monotonic(),
            "data": dict(payload),
        }

    payload["from_cache"] = False
    payload["cache_age"] = 0.0
    return payload


async def invalidate_strategy_cache(strategy_name: Optional[str] = None) -> None:
    """Invalidate cached strategy data.

    When ``strategy_name`` is ``None`` the entire cache is cleared. Otherwise only
    entries related to the provided strategy are removed.
    """

    global _executions_cache_lock, _daily_pnl_cache_lock, _performance_cache_lock

    if _executions_cache_lock is None:
        _executions_cache_lock = asyncio.Lock()
    if _daily_pnl_cache_lock is None:
        _daily_pnl_cache_lock = asyncio.Lock()
    if _performance_cache_lock is None:
        _performance_cache_lock = asyncio.Lock()

    async with _executions_cache_lock:
        if strategy_name is None:
            _executions_cache.clear()
        else:
            targets = [key for key in _executions_cache if key[0] == strategy_name]
            for target in targets:
                _executions_cache.pop(target, None)

    async with _daily_pnl_cache_lock:
        if strategy_name is None:
            _daily_pnl_cache.clear()
            for task in _daily_pnl_fetch_tasks.values():
                if not task.done():
                    task.cancel()
            _daily_pnl_fetch_tasks.clear()
        else:
            _daily_pnl_cache.pop(strategy_name, None)
            task = _daily_pnl_fetch_tasks.pop(strategy_name, None)
            if task and not task.done():
                task.cancel()

    async with _performance_cache_lock:
        if strategy_name is None:
            _performance_cache.clear()
            for task in _performance_fetch_tasks.values():
                if not task.done():
                    task.cancel()
            _performance_fetch_tasks.clear()
        else:
            prefix = f"{strategy_name}::"
            targets = [key for key in _performance_cache if key.startswith(prefix)]
            for key in targets:
                _performance_cache.pop(key, None)
                task = _performance_fetch_tasks.pop(key, None)
                if task and not task.done():
                    task.cancel()


def _clear_cache_without_lock(strategy_name: Optional[str] = None) -> None:
    if strategy_name is None:
        _executions_cache.clear()
        _daily_pnl_cache.clear()
        _performance_cache.clear()
        for task in _daily_pnl_fetch_tasks.values():
            if not task.done():
                task.cancel()
        _daily_pnl_fetch_tasks.clear()
        for task in _performance_fetch_tasks.values():
            if not task.done():
                task.cancel()
        _performance_fetch_tasks.clear()
        return

    targets = [key for key in _executions_cache if key[0] == strategy_name]
    for target in targets:
        _executions_cache.pop(target, None)
    _daily_pnl_cache.pop(strategy_name, None)
    task = _daily_pnl_fetch_tasks.pop(strategy_name, None)
    if task and not task.done():
        task.cancel()
    prefix = f"{strategy_name}::"
    perf_targets = [key for key in _performance_cache if key.startswith(prefix)]
    for key in perf_targets:
        _performance_cache.pop(key, None)
        task = _performance_fetch_tasks.pop(key, None)
        if task and not task.done():
            task.cancel()


def schedule_strategy_cache_invalidation(strategy_name: Optional[str] = None) -> None:
    """Best-effort helper for non-async contexts to invalidate caches."""

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            _clear_cache_without_lock(strategy_name)
            return
        if not loop.is_running():
            loop.run_until_complete(invalidate_strategy_cache(strategy_name))
            return
        loop.call_soon_threadsafe(asyncio.create_task, invalidate_strategy_cache(strategy_name))
        return

    asyncio.create_task(invalidate_strategy_cache(strategy_name))

@router.get("/api/strategy/settings")
async def get_strategy_settings(user: Optional[str] = Depends(get_current_user)):
    if not strategy_manager:
        raise HTTPException(status_code=500, detail="Strategy manager not initialized")
    return {"success": True, "settings": strategy_manager.get_settings()}

@router.post("/api/strategy/settings")
async def update_strategy_settings(settings: StrategySettingsRequest, user: Optional[str] = Depends(get_current_user)):
    if not strategy_manager:
        raise HTTPException(status_code=500, detail="Strategy manager not initialized")
    strategy_manager.update_settings(settings.model_dump())
    return {"success": True}

# --- Strategy Auth Token Generation Endpoint ---
from fastapi import Header

@router.post("/api/strategy/settings/generate-token")
async def generate_strategy_token(user: Optional[str] = Depends(get_current_user)):
    token = strategy_manager.generate_auth_token()
    return {"success": True, "token": token}

@router.get("/api/strategy/files")
async def list_strategy_files(user: Optional[str] = Depends(get_current_user)):
    """Get list of available strategy files from strategy directory"""
    if not strategy_manager:
        raise HTTPException(status_code=500, detail="Strategy manager not initialized")
    try:
        files = []
        strategy_dir = strategy_manager.strategy_dir
        for file_path in strategy_dir.glob("*.py"):
            if file_path.is_file():
                files.append(file_path.name)
        return {"success": True, "files": files}
    except Exception as e:
        logger.error(f"Failed to list strategy files: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/strategies")
async def list_strategies(user: Optional[str] = Depends(get_current_user)):
    if not strategy_manager:
        raise HTTPException(status_code=500, detail="Strategy manager not initialized")
    loop = asyncio.get_event_loop()
    try:
        strategies = await asyncio.wait_for(
            loop.run_in_executor(None, strategy_manager.get_strategies),
            timeout=6.0,
        )
    except asyncio.TimeoutError:
        logger.warning("list_strategies timed out; returning cached result")
        strategies = strategy_manager.get_strategies()
    return {"success": True, "strategies": strategies}

@router.post("/api/strategies")
async def add_strategy_api(strategy: StrategyRequest, user: Optional[str] = Depends(get_current_user)):
    if not strategy_manager:
        raise HTTPException(status_code=500, detail="Strategy manager not initialized")
    success = strategy_manager.add_strategy(strategy.model_dump())
    if not success:
        raise HTTPException(status_code=400, detail="Failed to add strategy")
    return {"success": True}

@router.get("/api/strategies/{name}")
async def get_strategy_api(name: str, user: Optional[str] = Depends(get_current_user)):
    if not strategy_manager:
        raise HTTPException(status_code=500, detail="Strategy manager not initialized")
    loop = asyncio.get_running_loop()
    try:
        strategy = await asyncio.wait_for(
            loop.run_in_executor(None, strategy_manager.get_strategy, name),
            timeout=5.0,
        )
    except asyncio.TimeoutError:
        logger.warning(f"get_strategy timed out for {name}; returning cached snapshot")
        strategy = strategy_manager.get_strategy_snapshot(name)
    except Exception as exc:
        logger.error(f"Failed to load strategy {name}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to load strategy") from exc
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return {"success": True, "strategy": strategy}

@router.get("/api/strategies/{name}/internal-info")
async def get_internal_strategy_info_api(name: str, user: Optional[str] = Depends(get_current_user)):
    if not strategy_manager:
        raise HTTPException(status_code=500, detail="Strategy manager not initialized")
    loop = asyncio.get_running_loop()
    try:
        info = await asyncio.wait_for(
            loop.run_in_executor(None, strategy_manager.get_internal_strategy_info, name),
            timeout=6.0,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "get_internal_strategy_info timed out; serving cached snapshot", strategy=name
        )
        cached = strategy_manager.get_cached_internal_strategy_info(name)
        if cached:
            return {"success": True, "strategy": cached, "cached": True}
        raise HTTPException(status_code=504, detail="Timed out loading strategy info")
    except Exception as exc:
        logger.error(f"Failed to load internal strategy info for {name}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to load strategy info") from exc

    if not info:
        cached = strategy_manager.get_cached_internal_strategy_info(name)
        if cached:
            return {"success": True, "strategy": cached, "cached": True}
        raise HTTPException(status_code=404, detail="Strategy not found")
    return {"success": True, "strategy": info}


@router.post("/api/strategies/{name}/internal-info/parameter")
async def update_strategy_parameter_api(
    name: str,
    payload: StrategyParameterUpdateRequest,
    user: Optional[str] = Depends(get_current_user),
):
    if not strategy_manager:
        raise HTTPException(status_code=500, detail="Strategy manager not initialized")

    try:
        parameter = strategy_manager.update_strategy_parameter(
            name, payload.parameter_name, payload.value
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(f"Failed to update strategy parameter for {name}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to update parameter") from exc

    return {"success": True, "parameter": parameter}


@router.get("/api/strategies/{name}/risk-settings")
async def get_strategy_risk_settings_api(name: str, user: Optional[str] = Depends(get_current_user)):
    if not strategy_manager:
        raise HTTPException(status_code=500, detail="Strategy manager not initialized")
    if name not in strategy_manager.strategies:
        raise HTTPException(status_code=404, detail="Strategy not found")
    settings = strategy_manager.get_strategy_risk_settings(name)
    return {"success": True, "settings": settings}


@router.put("/api/strategies/{name}/risk-settings")
async def update_strategy_risk_settings_api(
    name: str,
    payload: StrategyRiskSettings,
    user: Optional[str] = Depends(get_current_user),
): 
    if not strategy_manager:
        raise HTTPException(status_code=500, detail="Strategy manager not initialized")
    if name not in strategy_manager.strategies:
        raise HTTPException(status_code=404, detail="Strategy not found")
    try:
        updated = strategy_manager.update_strategy_risk_settings(name, payload.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(f"Failed to update strategy risk settings for {name}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to update risk settings") from exc
    return {"success": True, "settings": updated}


@router.get("/api/strategies/{name}/status")
async def get_internal_strategy_status_api(name: str, user: Optional[str] = Depends(get_current_user)):
    if not strategy_manager:
        raise HTTPException(status_code=500, detail="Strategy manager not initialized")
    loop = asyncio.get_event_loop()
    try:
        status = await asyncio.wait_for(
            loop.run_in_executor(None, strategy_manager.get_internal_strategy_status, name),
            timeout=5.0,
        )
    except asyncio.TimeoutError:
        logger.warning(f"get_internal_strategy_status timed out for {name}; returning cached state")
        status = strategy_manager.get_internal_strategy_status(name)
    if status is None:
        raise HTTPException(status_code=404, detail="Strategy status unavailable")
    return {"success": True, "status": status}

@router.post("/api/strategies/{name}/data-push")
async def set_strategy_data_push_api(
    name: str,
    payload: StrategyDataPushRequest,
    user: Optional[str] = Depends(get_current_user),
):
    if not strategy_manager:
        raise HTTPException(status_code=500, detail="Strategy manager not initialized")
    set_data_push = strategy_manager.set_data_push
    call_kwargs = {
        "enabled": payload.enabled,
        "subscription": payload.subscription,
        "symbol": payload.symbol,
    }

    try:
        signature = inspect.signature(set_data_push)
    except (TypeError, ValueError):
        signature = None

    if signature and "force_restart" in signature.parameters:
        call_kwargs["force_restart"] = bool(payload.force_restart)

    timeout_seconds = getattr(strategy_manager, "data_push_request_timeout", 8.0)
    timed_out = False
    result: Optional[Dict[str, Any]] = None
    background_task: Optional[asyncio.Task] = None

    try:
        background_task = asyncio.create_task(set_data_push(name, **call_kwargs))
    except RuntimeError:
        background_task = None
        result = await set_data_push(name, **call_kwargs)
    else:
        try:
            result = await asyncio.wait_for(
                asyncio.shield(background_task), timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            timed_out = True

            def _log_background_completion(
                fut: asyncio.Future, strategy: str = name
            ) -> None:
                try:
                    fut.result()
                except Exception as exc:  # pragma: no cover - defensive guard
                    logger.error(
                        "Background data push update failed",
                        strategy=strategy,
                        error=str(exc),
                    )

            background_task.add_done_callback(_log_background_completion)
            logger.warning(
                "Timed out waiting for strategy data push update; continuing in background",
                strategy=name,
                timeout=timeout_seconds,
            )
        except Exception:
            if background_task:
                background_task.cancel()
            raise

    if result is None:
        result = {}

    state_snapshot = strategy_manager.get_data_push_state(name) or {}
    if not state_snapshot:
        state_snapshot = result
    if not state_snapshot:
        state_snapshot = {}

    if timed_out:
        state_snapshot.setdefault("subscription", call_kwargs.get("subscription"))
        state_snapshot.setdefault("symbol", call_kwargs.get("symbol"))
        state_snapshot.setdefault("last_data_timestamp", None)
        if payload.enabled:
            state_snapshot.setdefault("active", True)
            state_snapshot.setdefault("awaiting_data", True)
            state_snapshot.setdefault("status_reason", "推送启动请求处理中")
        else:
            state_snapshot.setdefault("active", False)
            state_snapshot.setdefault("awaiting_data", False)
            state_snapshot.setdefault("status_reason", "推送停止请求处理中")

    state_payload = {
        "active": bool(state_snapshot.get("active")),
        "subscription": state_snapshot.get("subscription") or result.get("subscription"),
        "symbol": state_snapshot.get("symbol") or result.get("symbol"),
        "awaiting_data": bool(state_snapshot.get("awaiting_data")),
        "last_data_timestamp": state_snapshot.get("last_data_timestamp")
        or result.get("last_data_timestamp"),
        "status_reason": state_snapshot.get("status_reason") or result.get("status_reason"),
    }

    success = True
    error_message: Optional[str] = None
    if result.get("success") is False:
        success = False
        error_message = result.get("error") or "Failed to update data push state"

    response: Dict[str, Any] = {"success": success, "state": state_payload}
    if error_message:
        response["error"] = error_message
    if timed_out:
        response["pending"] = True

    return response

@router.put("/api/strategies/{name}")
async def update_strategy_api(name: str, updates: Dict[str, Any], user: Optional[str] = Depends(get_current_user)):
    if not strategy_manager:
        raise HTTPException(status_code=500, detail="Strategy manager not initialized")
    success = await strategy_manager.update_strategy(name, updates)
    if not success:
        raise HTTPException(status_code=400, detail="Failed to update strategy")
    return {"success": True}

@router.post("/api/strategies/{name}/rename")
async def rename_strategy_api(name: str, payload: StrategyRenameRequest, user: Optional[str] = Depends(get_current_user)):
    if not strategy_manager:
        raise HTTPException(status_code=500, detail="Strategy manager not initialized")
    ok = strategy_manager.rename_strategy(name, payload.new_name, rename_file=payload.rename_file or False)
    if not ok:
        raise HTTPException(status_code=400, detail="Failed to rename strategy")
    await invalidate_strategy_cache(name)
    await invalidate_strategy_cache(payload.new_name)
    return {"success": True}

@router.delete("/api/strategies/{name}")
async def delete_strategy_api(name: str, user: Optional[str] = Depends(get_current_user)):
    if not strategy_manager:
        raise HTTPException(status_code=500, detail="Strategy manager not initialized")
    success = strategy_manager.delete_strategy(name)
    if not success:
        raise HTTPException(status_code=404, detail="Strategy not found")
    await invalidate_strategy_cache(name)
    return {"success": True}

@router.post("/api/strategies/{name}/toggle")
async def toggle_strategy_api(name: str, enabled: bool, user: Optional[str] = Depends(get_current_user)):
    if not strategy_manager:
        raise HTTPException(status_code=500, detail="Strategy manager not initialized")
    success = await strategy_manager.update_strategy(name, {"enabled": enabled})
    if not success:
        raise HTTPException(status_code=400, detail="Failed to toggle strategy")
    return {"success": True}


@router.post("/api/strategies/{name}/reset")
async def reset_strategy_data_api(name: str, user: Optional[str] = Depends(get_current_user)):
    if not strategy_manager:
        raise HTTPException(status_code=500, detail="Strategy manager not initialized")
    try:
        result = strategy_manager.reset_strategy_data(name)
        await invalidate_strategy_cache(name)
        return {
            "success": True,
            "result": result,
            "message": f"Strategy {name} data reset",
        }
    except ValueError:
        raise HTTPException(status_code=404, detail="Strategy not found")
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.error(f"Reset strategy data error for {name}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to reset strategy data")


@router.get("/api/strategies/{name}/performance")
async def get_strategy_performance_api(name: str, range: str = "1d", start: Optional[str] = None, end: Optional[str] = None, user: Optional[str] = Depends(get_current_user)):
    if not strategy_manager:
        raise HTTPException(status_code=500, detail="Strategy manager not initialized")

    range_value = range or "custom"

    try:
        start_date: Optional[datetime] = None
        end_date: Optional[datetime] = None
        if start:
            try:
                start_date = parse_local_datetime(start)
            except Exception:
                start_date = None
        if end:
            try:
                end_date = parse_local_datetime(end)
            except Exception:
                end_date = None

        now = now_local()
        if range_value == "1d":
            start_date = now - timedelta(days=1)
            end_date = None
        elif range_value == "1w":
            start_date = now - timedelta(weeks=1)
            end_date = None
        elif range_value == "1m":
            start_date = now - timedelta(days=30)
            end_date = None
        elif range_value == "wtd":
            start_date = now - timedelta(days=now.weekday())
            end_date = None
        elif range_value == "mtd":
            start_date = now.replace(day=1)
            end_date = None
        elif range_value == "ytd":
            start_date = now.replace(month=1, day=1)
            end_date = None
        else:
            range_value = "custom"

        global _performance_cache_lock
        if _performance_cache_lock is None:
            _performance_cache_lock = asyncio.Lock()

        cache_key = _make_performance_cache_key(
            name, range_value, start_date, end_date
        )
        cached_entry: Optional[Dict[str, Any]] = None
        now_marker = monotonic()

        async with _performance_cache_lock:
            entry = _performance_cache.get(cache_key)
            if entry:
                age = now_marker - entry.get("timestamp", 0.0)
                if age < PERFORMANCE_CACHE_TTL:
                    cached_payload = dict(entry.get("data", {}))
                    cached_payload["from_cache"] = True
                    cached_payload["cache_age"] = age
                    return cached_payload
                cached_entry = dict(entry)

            fetch_task = _performance_fetch_tasks.get(cache_key)
            if not fetch_task or fetch_task.done():
                fetch_task = asyncio.create_task(
                    _load_strategy_performance(
                        name,
                        cache_key,
                        range_value=range_value,
                        start_date=start_date,
                        end_date=end_date,
                    )
                )

                def _cleanup(fut: asyncio.Future, key: str = cache_key) -> None:
                    _performance_fetch_tasks.pop(key, None)
                    if fut.cancelled():
                        return
                    try:
                        fut.result()
                    except Exception as exc:  # pragma: no cover - defensive guard
                        logger.warning(
                            "Strategy performance fetch task failed",
                            strategy=name,
                            error=str(exc),
                        )

                fetch_task.add_done_callback(_cleanup)
                _performance_fetch_tasks[cache_key] = fetch_task

        try:
            payload = await asyncio.wait_for(
                asyncio.shield(fetch_task), timeout=PERFORMANCE_FETCH_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Strategy performance fetch timed out; returning cached snapshot",
                strategy=name,
                range=range_value,
            )
            if cached_entry:
                cached_payload = dict(cached_entry.get("data", {})) or {"success": True, "metrics": {}}
                cached_payload["from_cache"] = True
                cached_payload["cache_age"] = now_marker - cached_entry.get("timestamp", now_marker)
                cached_payload["refresh_in_progress"] = True
                return cached_payload
            return {
                "success": True,
                "metrics": {},
                "from_cache": True,
                "refresh_in_progress": True,
            }
        except Exception as exc:
            logger.error(f"Get strategy performance error: {exc}")
            if cached_entry:
                cached_payload = dict(cached_entry.get("data", {})) or {"success": True, "metrics": {}}
                cached_payload["from_cache"] = True
                cached_payload["cache_warning"] = str(exc)
                return cached_payload
            return {"success": True, "metrics": {}}

        return payload
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.error(f"Unhandled strategy performance error: {exc}")
        return {"success": True, "metrics": {}}


@router.get("/api/strategies/{name}/pnl/daily")
async def get_strategy_daily_pnl(
    name: str,
    force_refresh: bool = False,
    user: Optional[str] = Depends(get_current_user),
):
    if 'order_manager' not in globals() or not order_manager:
        raise HTTPException(status_code=500, detail="Order manager not initialized")

    if not force_refresh:
        try:
            cached_rows = await cache_get_daily_pnl(name)
        except Exception:
            cached_rows = None
        if cached_rows:
            cache_age = 0.0
            try:
                stats_meta = await cache_get_strategy_stats(name)
            except Exception:
                stats_meta = {}
            updated_iso = stats_meta.get("updated_at") if isinstance(stats_meta, dict) else None
            if updated_iso:
                updated_dt = parse_local_datetime(updated_iso)
                if updated_dt is not None:
                    try:
                        age_seconds = (now_local() - updated_dt).total_seconds()
                        cache_age = max(0.0, float(age_seconds))
                    except Exception:
                        cache_age = 0.0
            return {
                "success": True,
                "pnl": cached_rows,
                "from_cache": True,
                "cache_age": cache_age,
            }

    if not force_refresh:
        try:
            cache_stats = await cache_get_strategy_stats(name)
        except Exception:
            cache_stats = {}
        if cache_stats and not cache_stats.get("stale"):
            try:
                pnl_rows = await cache_get_daily_pnl(name)
            except Exception:
                pnl_rows = None
            if pnl_rows is not None:
                return {
                    "success": True,
                    "pnl": pnl_rows,
                    "from_cache": True,
                    "cache_age": 0.0,
                }

    global _daily_pnl_cache_lock
    if _daily_pnl_cache_lock is None:
        _daily_pnl_cache_lock = asyncio.Lock()

    cache_key = name
    cached_entry: Optional[Dict[str, Any]] = None
    now = monotonic()

    async with _daily_pnl_cache_lock:
        entry = _daily_pnl_cache.get(cache_key)
        if entry and not force_refresh:
            age = now - entry.get("timestamp", 0.0)
            if age < DAILY_PNL_CACHE_TTL:
                cached_payload = dict(entry.get("data", {}))
                cached_payload["from_cache"] = True
                cached_payload["cache_age"] = age
                return cached_payload
        if entry:
            cached_entry = dict(entry)
        if force_refresh:
            _daily_pnl_cache.pop(cache_key, None)
            task = _daily_pnl_fetch_tasks.pop(cache_key, None)
            if task and not task.done():
                task.cancel()
        fetch_task = _daily_pnl_fetch_tasks.get(cache_key)
        if not fetch_task or fetch_task.done():
            fetch_task = asyncio.create_task(_load_strategy_daily_pnl(name, cache_key))

            def _cleanup(fut: asyncio.Future, key: str = cache_key) -> None:
                _daily_pnl_fetch_tasks.pop(key, None)
                if fut.cancelled():
                    return
                try:
                    fut.result()
                except Exception as exc:  # pragma: no cover - defensive guard
                    logger.warning(
                        "Strategy daily PnL fetch task failed",
                        strategy=name,
                        error=str(exc),
                    )

            fetch_task.add_done_callback(_cleanup)
            _daily_pnl_fetch_tasks[cache_key] = fetch_task

    try:
        payload = await asyncio.wait_for(
            asyncio.shield(fetch_task), timeout=DAILY_PNL_FETCH_TIMEOUT
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Strategy daily PnL fetch timed out; returning cached snapshot",
            strategy=name,
        )
        if cached_entry:
            cached_payload = dict(cached_entry.get("data", {})) or {"success": True, "pnl": []}
            cached_payload["from_cache"] = True
            cached_payload["cache_age"] = now - cached_entry.get("timestamp", now)
            cached_payload["refresh_in_progress"] = True
            return cached_payload
        return {
            "success": True,
            "pnl": [],
            "from_cache": True,
            "refresh_in_progress": True,
        }
    except Exception as exc:
        logger.error(f"Get strategy daily pnl error: {exc}")
        if cached_entry:
            cached_payload = dict(cached_entry.get("data", {})) or {"success": True, "pnl": []}
            cached_payload["from_cache"] = True
            cached_payload["cache_warning"] = str(exc)
            return cached_payload
        raise HTTPException(status_code=500, detail=str(exc))

    return payload

# External trigger endpoint - Async queue version
@router.post("/api/strategy")
async def external_strategy_order(payload: ExternalStrategyOrderRequest, req: Request):
    """策略订单接口 - 立即返回200，异步处理"""
    if not strategy_manager:
        raise HTTPException(status_code=500, detail="Strategy manager not initialized")
    
    # Auth check - strategy interface uses token in body or headers when enabled
    # Do this check FIRST to avoid any IBKR calls for unauthorized requests
    settings = strategy_manager.get_settings()
    if settings.get('enable_auth', False):
        # 1) Prefer body token
        token = payload.token
        # 2) Fallback to Authorization: Bearer <token>
        if not token:
            auth_header = req.headers.get('Authorization')
            if auth_header and auth_header.startswith('Bearer '):
                token = auth_header.split(' ', 1)[1]
        # 3) Fallback to X-Strategy-Token header
        if not token:
            token = req.headers.get('X-Strategy-Token')
        if not token or not strategy_manager.validate_auth_token(token):
            raise HTTPException(status_code=401, detail="Unauthorized")
    
    # Validate strategy exists and enabled - also before IBKR calls
    strategy = strategy_manager.get_strategy(payload.strategy)
    if not strategy or not strategy.get('enabled', False):
        raise HTTPException(status_code=400, detail="Strategy disabled or not found")
    
    # 基本参数验证
    if payload.type.upper() == "LIMIT" and not payload.price:
        raise HTTPException(status_code=400, detail="Price required for limit order")
    
    if payload.type.upper() not in ["MARKET", "LIMIT"]:
        raise HTTPException(status_code=400, detail="Invalid order type")
    
    # 将任务加入队列
    task_payload = {
        "strategy": payload.strategy,
        "symbol": payload.symbol,
        "action": payload.action,
        "qty": payload.qty,
        "type": payload.type,
        "price": payload.price,
        "exchange": payload.exchange,
        "sec_type": payload.sec_type
    }
    
    # 确保队列worker已启动
    await queue_manager.start_worker()
    
    task_id = queue_manager.add_task("strategy_order", task_payload)
    
    # 立即返回200
    return {
        "success": True,
        "task_id": task_id,
        "message": "Strategy order queued for processing",
        "status": "queued"
    }
@router.get("/api/strategies/{name}/executions")
async def get_strategy_executions(
    name: str,
    limit: int = 100,
    force_refresh: bool = False,
    user: Optional[str] = Depends(get_current_user),
):
    """Get strategy execution logs"""
    if not strategy_manager:
        raise HTTPException(status_code=500, detail="Strategy manager not initialized")

    effective_limit = max(1, min(int(limit or 1), 1000))

    global _executions_cache_lock
    if _executions_cache_lock is None:
        _executions_cache_lock = asyncio.Lock()

    cache_key = (name, effective_limit)
    cached_entry: Optional[Dict[str, Any]] = None
    now = monotonic()

    async with _executions_cache_lock:
        entry = _executions_cache.get(cache_key)
        if entry and not force_refresh:
            age = now - entry.get("timestamp", 0.0)
            if age < EXECUTIONS_CACHE_TTL:
                cached_payload = dict(entry.get("data", {}))
                cached_payload["from_cache"] = True
                cached_payload["cache_age"] = age
                return cached_payload
        if entry:
            cached_entry = dict(entry)
        if force_refresh:
            _executions_cache.pop(cache_key, None)
            task = _executions_fetch_tasks.pop(cache_key, None)
            if task and not task.done():
                task.cancel()
        fetch_task = _executions_fetch_tasks.get(cache_key)
        if not fetch_task or fetch_task.done():
            fetch_task = asyncio.create_task(
                _load_strategy_executions(name, effective_limit, cache_key)
            )

            def _cleanup(fut: asyncio.Future, key: Tuple[str, int] = cache_key) -> None:
                _executions_fetch_tasks.pop(key, None)
                if fut.cancelled():
                    return
                try:
                    fut.result()
                except Exception as exc:  # pragma: no cover - defensive guard
                    logger.warning(
                        "Strategy executions fetch task failed",
                        strategy=name,
                        error=str(exc),
                    )

            fetch_task.add_done_callback(_cleanup)
            _executions_fetch_tasks[cache_key] = fetch_task

    try:
        payload = await asyncio.wait_for(
            asyncio.shield(fetch_task), timeout=EXECUTIONS_FETCH_TIMEOUT
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Strategy executions fetch timed out; returning cached snapshot",
            strategy=name,
            limit=effective_limit,
        )
        if cached_entry:
            cached_payload = dict(cached_entry.get("data", {})) or {"success": True, "executions": []}
            cached_payload["from_cache"] = True
            cached_payload["cache_age"] = now - cached_entry.get("timestamp", now)
            cached_payload["refresh_in_progress"] = True
            return cached_payload
        return {
            "success": True,
            "executions": [],
            "from_cache": True,
            "refresh_in_progress": True,
        }
    except Exception as exc:
        logger.error(f"Get strategy executions error: {exc}")
        if cached_entry:
            cached_payload = dict(cached_entry.get("data", {})) or {"success": True, "executions": []}
            cached_payload["from_cache"] = True
            cached_payload["cache_warning"] = str(exc)
            return cached_payload
        raise HTTPException(status_code=500, detail=str(exc))

    return payload

# Clear all executions for a strategy
@router.delete("/api/strategies/{name}/executions")
async def clear_strategy_executions(name: str, user: Optional[str] = Depends(get_current_user)):
    """Clear all executions for a strategy"""
    try:
        db = get_db_manager()
        deleted = db.delete_strategy_orders(name)
        await invalidate_strategy_cache(name)
        await rebuild_account_daily_cache_from_db(db)
        return {"success": True, "deleted": int(deleted)}
    except Exception as e:
        logger.error(f"Clear strategy executions error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
