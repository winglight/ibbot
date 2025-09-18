from typing import List, Optional
import asyncio

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, Field

import core
from .auth import get_current_user
from src.dom_service import get_dom_service
from core import dom_config

router = APIRouter()
dom_service = get_dom_service()


class StartRequest(BaseModel):
    symbol: str
    num_rows: int = 5


@router.post("/api/dom/start")
async def dom_start(req: StartRequest, user: Optional[str] = Depends(get_current_user)):
    ok = dom_service.start(req.symbol, num_rows=req.num_rows)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to start depth stream")
    return {"success": True}


class StopRequest(BaseModel):
    symbol: str


@router.post("/api/dom/stop")
async def dom_stop(req: StopRequest, user: Optional[str] = Depends(get_current_user)):
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
async def update_dom_config(cfg: DOMConfigModel, user: Optional[str] = Depends(get_current_user)):
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
async def ack_alert(alert_id: int, user: Optional[str] = Depends(get_current_user)):
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

