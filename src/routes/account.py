import asyncio
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import JSONResponse
from loguru import logger

from src.utils import now_local

import core
from core import connection_manager
from .auth import get_current_user

router = APIRouter()

# Positions cache to reduce IBKR API calls
positions_cache = {}
positions_cache_timestamp = None
POSITIONS_CACHE_TTL = 5  # seconds


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
async def get_account_info(user: Optional[str] = Depends(get_current_user)):
    """Retrieve IBKR account information."""
    # 未连接时返回 400，并附带结构化错误信息
    if not connection_manager or not connection_manager.is_connected():
        last_err = None
        try:
            last_err = (
                connection_manager.get_last_connect_error()
                if connection_manager
                else None
            )
        except Exception:
            last_err = None
        logger.warning("Not connected to IBKR, returning 400 for account_info")
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "connected": False,
                "error": "Not connected to IBKR",
                "last_error": last_err,
            },
        )

    try:
        try:
            info = await asyncio.wait_for(
                core.ibkr_client.get_account_info(), timeout=10.0
            )
        except asyncio.TimeoutError:
            logger.warning("Account info request timed out")
            return {"success": False, "connected": True, "error": "Account info request timed out"}
        return {"success": True, "connected": True, "account_info": info}
    except Exception as e:
        logger.error(f"Get account info error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/positions")
async def get_positions(
    user: Optional[str] = Depends(get_current_user),
    fast: bool = True,
    use_cache: bool = True,
):
    """Retrieve current positions with optional caching."""
    global positions_cache, positions_cache_timestamp

    if not connection_manager.is_connected():
        logger.warning("Not connected to IBKR, returning empty positions list")
        return {
            "success": True,
            "positions": [],
            "from_cache": False,
            "warning": "Not connected to IBKR",
        }

    try:
        current_time = now_local()
        if use_cache and positions_cache and positions_cache_timestamp:
            cache_age = (current_time - positions_cache_timestamp).total_seconds()
            if cache_age < POSITIONS_CACHE_TTL:
                logger.debug(f"Using positions cache, age: {cache_age:.1f}s")
                return {
                    "success": True,
                    "positions": positions_cache,
                    "from_cache": True,
                }

        try:
            positions = await asyncio.wait_for(
                core.ibkr_client.get_positions(fast=fast), timeout=5.0
            )
            positions_cache = positions
            positions_cache_timestamp = current_time
            return {"success": True, "positions": positions, "from_cache": False}
        except asyncio.TimeoutError:
            if positions_cache:
                logger.warning(
                    "Positions request timed out, returning cached data"
                )
                return {
                    "success": True,
                    "positions": positions_cache,
                    "from_cache": True,
                    "cache_warning": "Request timed out, showing cached data",
                }
            else:
                logger.warning(
                    "Positions request timed out and no cache available"
                )
                return {
                    "success": False,
                    "error": "Positions request timed out",
                }

    except Exception as e:
        if positions_cache:
            logger.error(
                f"Get positions error: {e}, returning cached data"
            )
            return {
                "success": True,
                "positions": positions_cache,
                "from_cache": True,
                "cache_warning": "Error occurred, showing cached data",
            }
        logger.error(f"Get positions error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
