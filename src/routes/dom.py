from typing import Optional
import asyncio

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

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


@router.get("/api/config/dom")
async def get_dom_config(user: Optional[str] = Depends(get_current_user)):
    return {"window": dom_config.window, "max_levels": dom_config.max_levels}


@router.put("/api/config/dom")
async def update_dom_config(cfg: DOMConfigModel, user: Optional[str] = Depends(get_current_user)):
    dom_service.update_config(cfg.window, cfg.max_levels)
    return {
        "success": True,
        "config": {"window": dom_config.window, "max_levels": dom_config.max_levels},
    }


@router.get("/api/dom/data")
async def get_dom_data(user: Optional[str] = Depends(get_current_user)):
    return {"success": True, "metrics": dom_service.get_metrics()}


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
                    "metrics": dom_service.get_metrics(),
                    "events": dom_service.get_events(),
                }
            )
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        return

