from typing import List, Optional
import asyncio
from datetime import datetime

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

import core
from .auth import get_current_user
from src.dom_service import get_dom_service
from src.dom_optimizer import get_dom_optimization_orchestrator
from core import dom_config
from loguru import logger

router = APIRouter()
dom_service = get_dom_service()


def _ensure_not_read_only(request: Request) -> None:
    if getattr(request.state, "is_read_only", False):
        raise HTTPException(status_code=403, detail="Read-only access")


class StartRequest(BaseModel):
    symbol: str
    num_rows: int = 5


@router.post("/api/dom/start")
async def dom_start(
    req: StartRequest,
    request: Request,
    user: Optional[str] = Depends(get_current_user),
):
    _ensure_not_read_only(request)
    ok = dom_service.start(req.symbol, num_rows=req.num_rows)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to start depth stream")
    return {"success": True}


class StopRequest(BaseModel):
    symbol: str


@router.post("/api/dom/stop")
async def dom_stop(
    req: StopRequest,
    request: Request,
    user: Optional[str] = Depends(get_current_user),
):
    _ensure_not_read_only(request)
    ok = dom_service.stop(req.symbol)
    if not ok:
        raise HTTPException(status_code=400, detail="No active depth stream for symbol")
    return {"success": True}


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
    try:
        while True:
            await websocket.send_json(
                {
                    "symbol": symbol,
                    "metrics": dom_service.get_metrics(symbol),
                    "events": dom_service.get_events(),
                }
            )
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
    response = dom_service.get_stats(
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
