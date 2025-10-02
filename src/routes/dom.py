from typing import Any, Dict, List, Optional
import asyncio
from datetime import datetime
import inspect
import json

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, Field

from .auth import get_current_user
from src.dom_optimizer import get_dom_optimization_orchestrator
from core import get_core_container
from loguru import logger

router = APIRouter()
core_container = get_core_container()
dom_service = core_container.dom_service
dom_config = core_container.dom_config

DOM_STOP_TIMEOUT = 2.5


def _ensure_not_read_only(request: Request) -> None:
    if getattr(request.state, "is_read_only", False):
        raise HTTPException(status_code=403, detail="Read-only access")


class StartRequest(BaseModel):
    symbol: str
    num_rows: int = Field(default=5, ge=1, le=30)
    force_restart: bool = False


@router.post("/api/dom/start")
async def dom_start(
    req: StartRequest,
    request: Request,
    user: Optional[str] = Depends(get_current_user),
):
    _ensure_not_read_only(request)
    symbol = (req.symbol or "").strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="Symbol is required")

    force_restart = bool(req.force_restart)

    if dom_service.is_stream_active(symbol) and not force_restart:
        return {"success": True, "pending": False, "already_running": True}

    if dom_service.is_start_in_progress(symbol) and not force_restart:
        return {"success": True, "pending": True, "starting": True}

    try:
        start_task = dom_service.request_stream_start(
            symbol, num_rows=req.num_rows, force_restart=force_restart
        )
    except RuntimeError:
        ok = await dom_service.start_stream(symbol, num_rows=req.num_rows, force_restart=force_restart)
        if not ok:
            error_message, status_code = dom_service.get_last_start_error()
            detail = error_message or "Failed to start depth stream"
            status = (
                status_code
                if isinstance(status_code, int) and 400 <= status_code < 600
                else 500
            )
            raise HTTPException(status_code=status, detail=detail)
        return {"success": True, "pending": False}

    try:
        ok = await asyncio.wait_for(asyncio.shield(start_task), timeout=1.2)
    except asyncio.TimeoutError:
        return {"success": True, "pending": True, "starting": True}
    except Exception as exc:
        logger.error("DOM start task failed", symbol=symbol, error=str(exc))
        raise HTTPException(status_code=500, detail="Failed to start depth stream") from exc

    if not ok:
        error_message, status_code = dom_service.get_last_start_error()
        detail = error_message or "Failed to start depth stream"
        status = status_code if isinstance(status_code, int) and 400 <= status_code < 600 else 500
        raise HTTPException(status_code=status, detail=detail)
    return {"success": True, "pending": False, "already_running": False}


class StopRequest(BaseModel):
    symbol: str


@router.post("/api/dom/stop")
async def dom_stop(
    req: StopRequest,
    request: Request,
    user: Optional[str] = Depends(get_current_user),
):
    _ensure_not_read_only(request)
    symbol = (req.symbol or "").strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="Symbol is required")
    stop_task = asyncio.create_task(dom_service.stop_stream(symbol))
    try:
        ok = await asyncio.wait_for(asyncio.shield(stop_task), timeout=DOM_STOP_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning(
            "DOM stop request timed out; continuing in background",
            symbol=symbol,
        )

        def _log_result(fut: asyncio.Future) -> None:
            try:
                result = fut.result()
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.warning(
                    "Background DOM stop task failed",
                    symbol=symbol,
                    error=str(exc),
                )
            else:
                if result:
                    logger.info(
                        "DOM stop completed asynchronously",
                        symbol=symbol,
                    )
                else:
                    logger.warning(
                        "DOM stop background task reported no active stream",
                        symbol=symbol,
                    )

        stop_task.add_done_callback(_log_result)
        return {"success": True, "pending": True}
    except Exception as exc:
        stop_task.cancel()
        logger.error("DOM stop task failed", symbol=symbol, error=str(exc))
        raise HTTPException(status_code=500, detail="Failed to stop depth stream") from exc

    if not ok:
        raise HTTPException(status_code=400, detail="No active depth stream for symbol")
    return {"success": True, "pending": False}


class DOMConfigModel(BaseModel):
    window: int = dom_config.window
    max_levels: int = dom_config.max_levels
    trend_thresholds: List[float] = Field(
        default_factory=lambda: list(getattr(dom_config, "trend_thresholds", []))
    )


@router.get("/api/config/dom")
async def get_dom_config(user: Optional[str] = Depends(get_current_user)):
    return dom_service.get_config()


@router.put("/api/config/dom")
async def update_dom_config(
    cfg: DOMConfigModel,
    request: Request,
    user: Optional[str] = Depends(get_current_user),
):
    _ensure_not_read_only(request)
    dom_service.update_config(cfg.window, cfg.max_levels, cfg.trend_thresholds)
    return {
        "success": True,
        "config": dom_service.get_config(),
    }


@router.get("/api/dom/data")
async def get_dom_data(
    symbol: Optional[str] = None,
    user: Optional[str] = Depends(get_current_user),
):
    return {
        "success": True,
        "metrics": dom_service.get_metrics(symbol),
        "depth": dom_service.get_last_depth(symbol),
    }


@router.get("/api/alerts")
async def get_alerts(user: Optional[str] = Depends(get_current_user)):
    return {"success": True, "alerts": dom_service.list_alerts()}


@router.post("/api/alerts/{alert_id}/ack")
async def ack_alert(
    alert_id: int,
    request: Request,
    user: Optional[str] = Depends(get_current_user),
):
    _ensure_not_read_only(request)
    ok = dom_service.ack_alert(alert_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Alert not found")
    return {"success": True}


@router.websocket("/ws/dom/{symbol}")
async def dom_ws(websocket: WebSocket, symbol: str):
    await websocket.accept()

    def _make_signature(value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, dict) and not value:
            return None
        try:
            return json.dumps(value, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return repr(value)

    last_metrics_signature: Optional[str] = None
    last_events_signature: Optional[str] = None
    last_error: Optional[str] = None
    try:
        while True:
            metrics = dom_service.get_metrics(symbol)

            events_provider = dom_service.get_events
            events: Any
            try:
                provider_signature = inspect.signature(events_provider)
            except (TypeError, ValueError):
                provider_signature = None

            if provider_signature:
                accepts_positional = any(
                    param.kind in (
                        inspect.Parameter.POSITIONAL_ONLY,
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        inspect.Parameter.VAR_POSITIONAL,
                    )
                    for param in provider_signature.parameters.values()
                )
                if accepts_positional:
                    events = events_provider(symbol)
                else:
                    events = events_provider()
            else:
                try:
                    events = events_provider(symbol)
                except TypeError:
                    events = events_provider()

            metrics_signature = _make_signature(metrics)
            events_signature = _make_signature(events)

            if metrics_signature and metrics_signature != last_metrics_signature:
                await websocket.send_json(
                    {
                        "symbol": symbol,
                        "metrics": metrics,
                        "events": events,
                    }
                )
                last_metrics_signature = metrics_signature
                last_events_signature = events_signature
                last_error = None
            else:
                if not metrics_signature:
                    error_message = "DOM metrics unavailable"
                else:
                    error_message = "DOM metrics stale: no new data received"

                if last_error != error_message or events_signature != last_events_signature:
                    await websocket.send_json(
                        {
                            "symbol": symbol,
                            "metrics": None,
                            "events": events,
                            "error": error_message,
                        }
                    )
                    last_error = error_message
                    last_events_signature = events_signature
                    if not metrics_signature:
                        last_metrics_signature = None

            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        return


@router.get("/api/dom/history/{symbol}")
async def get_dom_history(
    symbol: str,
    limit: int = Query(500, ge=0),
    start: Optional[str] = None,
    end: Optional[str] = None,
    include_snapshots: bool = Query(True),
    user: Optional[str] = Depends(get_current_user),
):
    history = dom_service.get_history(
        symbol,
        limit=None if limit == 0 else limit,
        start_time=start,
        end_time=end,
        include_snapshots=include_snapshots,
    )
    return {"success": True, "symbol": symbol, **history}


@router.get("/api/dom/stats/{symbol}")
async def get_dom_stats(
    symbol: str,
    lookback_seconds: Optional[int] = Query(None, ge=60),
    start: Optional[str] = None,
    end: Optional[str] = None,
    max_points: Optional[int] = Query(None, ge=10, le=5000),
    user: Optional[str] = Depends(get_current_user),
):
    response = await dom_service.get_stats_async(
        symbol,
        lookback_seconds=lookback_seconds,
        start_time=start,
        end_time=end,
        max_points=max_points,
    )
    response["success"] = True
    return response


class OptimizeRequest(BaseModel):
    symbol: str
    start: Optional[str] = None
    end: Optional[str] = None
    limit: Optional[int] = Field(default=None, ge=0)
    apply: bool = True
    min_signals: int = Field(default=0, ge=0)
    obi_candidates: Optional[List[float]] = None
    ofi_candidates: Optional[List[float]] = None
    trend_candidates: Optional[List[float]] = None
    feature_thresholds: Optional[Dict[str, Dict[str, float]]] = None


@router.post("/api/dom/optimize")
async def optimize_dom_parameters(
    req: OptimizeRequest,
    request: Request,
    user: Optional[str] = Depends(get_current_user),
):
    _ensure_not_read_only(request)
    orchestrator = get_dom_optimization_orchestrator()
    try:
        result = orchestrator.run_optimization(
            req.symbol,
            start_time=req.start,
            end_time=req.end,
            limit=req.limit,
            apply_updates=req.apply,
            min_signals=req.min_signals,
            obi_candidates=req.obi_candidates,
            ofi_candidates=req.ofi_candidates,
            trend_candidates=req.trend_candidates,
            feature_thresholds=req.feature_thresholds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.exception("DOM optimisation request failed")
        raise HTTPException(status_code=500, detail="DOM optimisation failed") from exc
    return {"success": True, **result}


class AutomationScheduleRequest(BaseModel):
    enabled: bool = True
    interval_minutes: Optional[int] = Field(default=None, ge=0)
    start_at: Optional[datetime] = None
    symbol: Optional[str] = None
    lookback_minutes: Optional[int] = Field(default=None, ge=0)
    limit: Optional[int] = Field(default=None, ge=0)
    apply_updates: Optional[bool] = None
    min_signals: Optional[int] = Field(default=None, ge=0)
    obi_candidates: Optional[List[float]] = None
    ofi_candidates: Optional[List[float]] = None
    feature_thresholds: Optional[Dict[str, Dict[str, float]]] = None


@router.post("/api/dom/automation/schedule")
async def configure_dom_automation(
    req: AutomationScheduleRequest,
    request: Request,
    user: Optional[str] = Depends(get_current_user),
):
    _ensure_not_read_only(request)
    orchestrator = get_dom_optimization_orchestrator()
    try:
        status = orchestrator.configure_schedule(
            enabled=req.enabled,
            interval_minutes=req.interval_minutes,
            start_at=req.start_at,
            symbol=req.symbol,
            lookback_minutes=req.lookback_minutes,
            limit=req.limit,
            apply_updates=req.apply_updates,
            min_signals=req.min_signals,
            obi_candidates=req.obi_candidates,
            ofi_candidates=req.ofi_candidates,
            feature_thresholds=req.feature_thresholds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"success": True, **status}


@router.get("/api/dom/automation/status")
async def get_dom_automation_status(
    user: Optional[str] = Depends(get_current_user),
):
    orchestrator = get_dom_optimization_orchestrator()
    return {"success": True, **orchestrator.get_status()}
