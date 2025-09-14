from typing import Optional
import asyncio

from fastapi import APIRouter, HTTPException, Depends, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

import core
from .auth import get_current_user
from core import connection_manager, dom_processor, depth_subscriber, dom_config

router = APIRouter()


class StartRequest(BaseModel):
    symbol: str
    num_rows: int = 5


@router.post("/api/dom/start")
async def dom_start(req: StartRequest, user: Optional[str] = Depends(get_current_user)):
    if not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    ok = depth_subscriber.start_depth_stream(
        req.symbol, dom_processor.on_depth_update, num_rows=req.num_rows
    )
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to start depth stream")
    return {"success": True}


class StopRequest(BaseModel):
    symbol: str


@router.post("/api/dom/stop")
async def dom_stop(req: StopRequest, user: Optional[str] = Depends(get_current_user)):
    ok = depth_subscriber.stop_depth_stream(req.symbol)
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
    dom_config.window = cfg.window
    dom_config.max_levels = cfg.max_levels
    core.dom_processor = core.DOMProcessor(
        window=dom_config.window, max_levels=dom_config.max_levels
    )
    return {
        "success": True,
        "config": {"window": dom_config.window, "max_levels": dom_config.max_levels},
    }


@router.get("/api/dom/data")
async def get_dom_data(user: Optional[str] = Depends(get_current_user)):
    return {"success": True, "metrics": dom_processor.last_metrics}


@router.websocket("/ws/dom/{symbol}")
async def dom_ws(websocket: WebSocket, symbol: str):
    await websocket.accept()
    try:
        while True:
            await websocket.send_json(
                {"symbol": symbol, "metrics": dom_processor.last_metrics}
            )
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        return

