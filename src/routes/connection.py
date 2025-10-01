import asyncio
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger

import core
from core import (
    connection_manager,
    risk_manager,
    strategy_manager,
    queue_manager,
    websocket_manager,
    order_manager,
    notification_manager,
)
from src.connection_manager import ConnectionConfig
from src.ib_command_health import (
    IB_COMMAND_HEALTH_KEY,
    IB_COMMAND_HEALTH_STALE_AFTER_SEC,
    build_ib_connection_health_payload,
    resolve_ib_account_type,
)
from .auth import audit_log_failure, audit_log_success, get_current_user, require_roles

router = APIRouter()


def _decode_redis_mapping(raw: Dict[Any, Any]) -> Dict[str, str]:
    """Convert Redis hash payload into a plain string dictionary."""

    result: Dict[str, str] = {}
    for key, value in raw.items():
        if key is None:
            continue
        if isinstance(key, (bytes, bytearray)):
            key = key.decode("utf-8", errors="ignore")
        else:
            key = str(key)
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8", errors="ignore")
        elif value is None:
            value = ""
        else:
            value = str(value)
        result[key] = value
    return result


async def _load_ib_command_health() -> Dict[str, Any] | None:
    """Load ib-command service health details from Redis if available."""

    container = core.get_container()
    redis_client = getattr(container, "redis_client", None) if container else None
    if redis_client is None:
        return None

    try:
        raw = await redis_client.hgetall(IB_COMMAND_HEALTH_KEY)
    except Exception as exc:  # pragma: no cover - redis access errors
        logger.debug("Failed to load ib-command health from Redis: {}", exc)
        return None

    if not raw:
        return None

    stale_after = (
        IB_COMMAND_HEALTH_STALE_AFTER_SEC
        if IB_COMMAND_HEALTH_STALE_AFTER_SEC > 0
        else 30.0
    )
    return build_ib_connection_health_payload(
        _decode_redis_mapping(raw),
        stale_after=stale_after,
    )


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
async def connect_ibkr(
    config: ConnectionConfig,
    request: Request,
    user: Optional[str] = Depends(require_roles("admin", "operator", action="connection.connect")),
):
    try:
        loop = asyncio.get_event_loop()
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, lambda: connection_manager.disconnect()),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            logger.error("Disconnection operation timed out before reconnect")
            await audit_log_failure(request, detail="disconnect timeout before connect")
            raise HTTPException(status_code=408, detail="Disconnect request timed out")

        # 更新连接配置在断开之后，确保新参数生效
        connection_manager.update_config(config)

        try:
            success = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: connection_manager.connect()),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            logger.error("Connection operation timed out")
            await audit_log_failure(request, detail="connection operation timed out")
            raise HTTPException(status_code=408, detail="Connection operation timed out")
        except Exception as exc:
            logger.warning("Connection attempt failed", error=str(exc))
            detail = None
            try:
                detail = connection_manager.get_last_connect_error()
            except Exception:
                detail = None
            if not detail:
                detail = str(exc) or "Failed to connect to IBKR"
            await audit_log_failure(request, detail=str(detail))
            raise HTTPException(status_code=400, detail=detail)

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
                    risk_manager=risk_manager,
                    strategy_manager=strategy_manager,
                    websocket_manager=websocket_manager,
                    order_manager=order_manager,
                    notification_manager=notification_manager,
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
                info = await ibkr_client_local.get_account_info()
                await websocket_manager.broadcast({
                    "type": "account_update",
                    "account_info": info
                })
            except asyncio.TimeoutError:
                logger.warning("Broadcast account_info snapshot timed out after connect")
            except Exception as e:
                logger.warning(f"Failed to broadcast account_info snapshot after connect: {e}")

            await audit_log_success(
                request,
                detail="connected to IBKR",
                extra={"paper_trading": connection_manager.config.paper_trading},
            )
            return {"success": True, "message": "Connected successfully"}
        else:
            await audit_log_failure(request, detail="connection returned unsuccessful state")
            raise HTTPException(status_code=400, detail="Failed to connect to IBKR")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Connection error: {e}")
        await audit_log_failure(request, detail=str(e))
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/disconnect")
async def disconnect_ibkr(
    request: Request,
    user: Optional[str] = Depends(require_roles("admin", "operator", action="connection.disconnect")),
):
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

        await audit_log_success(request, detail="disconnected from IBKR")
        return {"success": True, "message": "Disconnected successfully"}
    except Exception as e:
        logger.error(f"Disconnection error: {e}")
        await audit_log_failure(request, detail=str(e))
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/connection_status")
async def get_connection_status(user: Optional[str] = Depends(get_current_user)):
    """Return the current IBKR connection status with a guarded timeout.

    In production we have observed rare cases where the low level IB client can
    block when checking the connection state (for example when the background
    connection thread is stuck).  Previously this would cause the whole HTTP
    request to hang until the frontend aborted it after several seconds,
    resulting in a timeout error.  To keep the API responsive we perform the
    status check in a worker thread and cap the wait time; if the check cannot
    finish in time we fall back to the last known state instead of hanging the
    request.
    """

    try:
        redis_health = await _load_ib_command_health()
        if redis_health and not redis_health.get("stale", False):
            return redis_health

        try:
            # Guard against rare blocking behaviour inside the IB client by
            # executing the check in a worker thread with a small timeout.
            is_connected = await asyncio.wait_for(
                asyncio.to_thread(connection_manager.is_connected),
                timeout=1.0,
            )
        except asyncio.TimeoutError:
            logger.warning("Connection status check timed out; using cached state")
            is_connected = getattr(connection_manager, "connected", False)
        except Exception as exc:
            logger.warning(f"Connection status check failed: {exc}")
            is_connected = getattr(connection_manager, "connected", False)

        redis_snapshot = dict(redis_health) if redis_health else None

        account_type = None
        if redis_snapshot:
            account_type = redis_snapshot.get("account_type") or None
        if not account_type:
            account_type = "Paper"
            try:
                if not connection_manager.config.paper_trading:
                    account_type = "Live"
            except Exception as exc:
                logger.debug(f"Failed to resolve account type: {exc}")
                account_type = resolve_ib_account_type()

        response: Dict[str, Any] = {
            "connected": bool(is_connected),
            "account_type": account_type,
            "source": "http",
            "stale": False,
        }

        if redis_snapshot:
            response["redis"] = redis_snapshot

        try:
            last_error = connection_manager.get_last_connect_error()
            if last_error:
                response["last_error"] = last_error
        except Exception:
            pass

        return response
    except Exception as e:
        logger.error(f"Failed to get connection status: {e}")
        raise HTTPException(status_code=500, detail="Failed to get connection status")
