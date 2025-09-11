import asyncio
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends
from loguru import logger

import core
from core import connection_manager, risk_manager, strategy_manager, queue_manager, websocket_manager, order_manager
from src.connection_manager import ConnectionConfig
from .auth import get_current_user

router = APIRouter()

@router.get("/api/connection_config")
async def get_connection_config():
    try:
        default_config = connection_manager.get_config()
        return {
            "host": default_config.host,
            "port": default_config.port,
            "client_id": default_config.client_id,
            "paper_trading": default_config.paper_trading,
        }
    except Exception as e:
        logger.error(f"Failed to get connection config: {e}")
        raise HTTPException(status_code=500, detail="Failed to get connection config")

@router.post("/api/connect")
async def connect_ibkr(config: ConnectionConfig, user: Optional[str] = Depends(get_current_user)):
    try:
        connection_manager.update_config(config)
        loop = asyncio.get_event_loop()
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, lambda: connection_manager.disconnect()),
                timeout=15.0,
            )
            success = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: connection_manager.connect()),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            logger.error("Connection operation timed out")
            raise HTTPException(status_code=408, detail="Connection operation timed out")

        if success:
            if core.ibkr_client is None:
                ibkr_client_local = core.IBKRClient()
            else:
                ibkr_client_local = core.ibkr_client
            try:
                risk_manager.ibkr_client = ibkr_client_local
                try:
                    risk_manager.set_dependencies(order_manager=order_manager)
                except Exception as _rm_dep_err:
                    logger.warning(f"RiskManager set_dependencies after connect failed: {_rm_dep_err}")
                logger.info("Risk manager ibkr_client updated after connection")
            except Exception as re_err:
                logger.warning(f"RiskManager ibkr_client update warning: {re_err}")
            try:
                queue_manager.set_dependencies(
                    ibkr_client=ibkr_client_local,
                    risk_manager=risk_manager,
                    strategy_manager=strategy_manager,
                    connection_manager=connection_manager,
                    websocket_manager=websocket_manager,
                    order_manager=order_manager,
                )
            except Exception as dep_err:
                logger.warning(f"QueueManager set_dependencies warning: {dep_err}")
            core.ibkr_client = ibkr_client_local

            # 主动广播连接状态，供前端立即更新UI
            try:
                account_type = "Paper" if connection_manager.config.paper_trading else "Live"
                await websocket_manager.broadcast({
                    "type": "connection_status",
                    "connected": True,
                    "account_type": account_type,
                })
            except Exception as be:
                logger.warning(f"Failed to broadcast connection status after connect: {be}")

            # 广播账户信息快照，供前端取消HTTP轮询
            try:
                info = await asyncio.wait_for(ibkr_client_local.get_account_info(), timeout=8.0)
                await websocket_manager.broadcast({
                    "type": "account_update",
                    "account_info": info
                })
            except asyncio.TimeoutError:
                logger.warning("Broadcast account_info snapshot timed out after connect")
            except Exception as e:
                logger.warning(f"Failed to broadcast account_info snapshot after connect: {e}")

            return {"success": True, "message": "Connected successfully"}
        else:
            raise HTTPException(status_code=400, detail="Failed to connect to IBKR")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Connection error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/disconnect")
async def disconnect_ibkr(user: Optional[str] = Depends(get_current_user)):
    try:
        if risk_manager:
            risk_manager.stop()
        connection_manager.disconnect()

        # 主动广播断开状态，供前端立即更新UI
        try:
            account_type = "Paper" if connection_manager.config.paper_trading else "Live"
            await websocket_manager.broadcast({
                "type": "connection_status",
                "connected": False,
                "account_type": account_type,
            })
        except Exception as be:
            logger.warning(f"Failed to broadcast connection status after disconnect: {be}")

        # 广播未连接的账户占位快照
        try:
            last_err = None
            try:
                last_err = connection_manager.get_last_connect_error()
            except Exception:
                last_err = None
            await websocket_manager.broadcast({
                "type": "account_update",
                "account_info": {"connected": False, "last_error": last_err}
            })
        except Exception as e:
            logger.warning(f"Failed to broadcast account_update after disconnect: {e}")

        return {"success": True, "message": "Disconnected successfully"}
    except Exception as e:
        logger.error(f"Disconnection error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/connection_status")
async def get_connection_status(user: Optional[str] = Depends(get_current_user)):
    return {
        "connected": connection_manager.is_connected(),
        "account_type": "Paper" if connection_manager.config.paper_trading else "Live",
    }
