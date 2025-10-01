import asyncio
import os
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional, Dict, Tuple, Any, Mapping, MutableMapping, Callable, Awaitable

from fastapi import APIRouter, HTTPException, Depends, Request
from loguru import logger

import core
from core import connection_manager
from .auth import (
    audit_log_failure,
    audit_log_success,
    get_current_user,
    require_roles,
)
from src.account_snapshot_cache import (
    read_account_info_snapshot,
    read_positions_snapshot,
)
from src.ibbot.redis import DEFAULT_COMMAND_QUEUE, enqueue_ib_command, read_hash
from src.models import OrderRequest
from src.utils import ensure_local_timezone, now_local, parse_local_datetime, to_local_iso
from src.ib_command_health import (
    IB_COMMAND_HEALTH_KEY,
    IB_COMMAND_HEALTH_STALE_AFTER_SEC,
    build_ib_connection_health_payload,
)

router = APIRouter()

NY_TZ = ZoneInfo("America/New_York")

_COMMAND_QUEUE_OVERRIDE = os.getenv("IB_COMMAND_QUEUE_KEY", "").strip() or None
_WEBSOCKET_BROADCAST_OVERRIDE: Optional[
    Callable[[Mapping[str, Any]], Awaitable[None]]
] = None


def _get_order_manager():
    container = core.get_container()
    manager = getattr(container, "order_manager", None) if container else None
    if manager is not None:
        return manager
    try:
        return core.order_manager  # type: ignore[attr-defined]
    except AttributeError:
        return None


async def _resolve_command_queue_key() -> str:
    """Return the Redis list key used for IB command dispatching."""

    if _COMMAND_QUEUE_OVERRIDE:
        return _COMMAND_QUEUE_OVERRIDE

    container = core.get_container()
    provider = getattr(container, "config_provider", None) if container else None
    if provider is None:
        return DEFAULT_COMMAND_QUEUE

    try:
        queue_key = await provider.get(
            "ib-command",
            "queue_key",
            default=DEFAULT_COMMAND_QUEUE,
        )
    except Exception:  # pragma: no cover - config lookup failure
        return DEFAULT_COMMAND_QUEUE

    queue_key_str = str(queue_key or "").strip()
    return queue_key_str or DEFAULT_COMMAND_QUEUE


async def _enqueue_command(payload: Mapping[str, Any]) -> None:
    """Push the command payload onto the configured Redis queue."""

    queue_key = await _resolve_command_queue_key()
    await enqueue_ib_command(payload, queue_key=queue_key)


def set_websocket_broadcast_override(
    broadcaster: Optional[Callable[[Mapping[str, Any]], Awaitable[None]]]
) -> None:
    """Configure a test-only websocket broadcast override."""

    global _WEBSOCKET_BROADCAST_OVERRIDE
    _WEBSOCKET_BROADCAST_OVERRIDE = broadcaster


def _resolve_websocket_broadcaster() -> Optional[Callable[[Mapping[str, Any]], Awaitable[None]]]:
    if _WEBSOCKET_BROADCAST_OVERRIDE is not None:
        return _WEBSOCKET_BROADCAST_OVERRIDE

    container = core.get_container()
    manager = getattr(container, "websocket_manager", None) if container else None
    broadcast = getattr(manager, "broadcast", None) if manager else None
    if callable(broadcast):
        return broadcast

    try:
        manager = core.websocket_manager  # type: ignore[attr-defined]
    except AttributeError:
        return None

    broadcast = getattr(manager, "broadcast", None)
    if callable(broadcast):
        return broadcast
    return None


async def _broadcast_event(message: Mapping[str, Any]) -> None:
    broadcaster = _resolve_websocket_broadcaster()
    if broadcaster is None:
        return
    try:
        await broadcaster(dict(message))
    except Exception:
        logger.debug("Failed to broadcast websocket event", exc_info=True)


async def _load_positions_snapshot() -> Tuple[list[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Return cached positions and the raw snapshot wrapper when available."""

    snapshot = await read_positions_snapshot()
    if not snapshot:
        return [], None
    data = snapshot.get("data")
    if isinstance(data, list):
        return data, snapshot
    return [], snapshot


async def _load_account_snapshot() -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Return cached account info and the snapshot wrapper when available."""

    snapshot = await read_account_info_snapshot()
    if not snapshot:
        return None, None
    data = snapshot.get("data")
    if isinstance(data, dict):
        return data, snapshot
    return None, snapshot


def _normalise_position_quantity(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


async def _ensure_ib_command_available() -> MutableMapping[str, Any]:
    """Ensure ib-command health data indicates the queue is available."""

    container = core.get_container()
    redis_client = getattr(container, "redis_client", None) if container else None

    try:
        raw = await read_hash(IB_COMMAND_HEALTH_KEY, redis=redis_client)
    except Exception as exc:
        logger.debug("Failed to read ib-command health: {}", exc)
        raw = {}

    if not raw:
        raise HTTPException(status_code=503, detail="ib-command service unavailable")

    stale_after = IB_COMMAND_HEALTH_STALE_AFTER_SEC or 30
    payload = build_ib_connection_health_payload(raw, stale_after=stale_after)
    if not payload.get("connected"):
        raise HTTPException(status_code=503, detail="ib-command service unavailable")
    return payload


def _extract_available_funds(account_info: Any) -> Optional[float]:
    """Best-effort extraction of available funds from cached account data."""

    if account_info is None:
        return None

    if isinstance(account_info, dict):
        for key in ("available_funds", "availableFunds", "availableFundsBase"):
            if key in account_info:
                try:
                    return float(account_info[key])
                except (TypeError, ValueError):
                    continue

        for nested_key in ("account_summary", "summary", "accountInfo", "account_info"):
            nested = account_info.get(nested_key)
            if nested is not None and nested is not account_info:
                result = _extract_available_funds(nested)
                if result is not None:
                    return result

    if isinstance(account_info, list):
        for item in account_info:
            if not isinstance(item, dict):
                continue
            tag = str(item.get("tag") or "").lower()
            if tag in {"availablefunds", "availablefunds-base", "available_funds"}:
                try:
                    return float(item.get("value", 0))
                except (TypeError, ValueError):
                    continue

    return None

@router.post("/api/place_order")
async def place_order(
    order: OrderRequest,
    request: Request,
    user: Optional[str] = Depends(require_roles("admin", "operator", action="orders.place")),
):
    """下单"""
    order_type = (order.order_type or "").upper()
    if order_type not in {"MARKET", "LIMIT"}:
        await audit_log_failure(request, detail=f"invalid order type: {order.order_type}")
        raise HTTPException(status_code=400, detail="Invalid order type")
    if order_type == "LIMIT" and not order.price:
        await audit_log_failure(request, detail="limit order missing price")
        raise HTTPException(status_code=400, detail="Price required for limit order")

    command_id = uuid.uuid4().hex
    method = "place_market_order" if order_type == "MARKET" else "place_limit_order"
    kwargs: Dict[str, Any] = {
        "symbol": order.symbol,
        "quantity": order.quantity,
        "action": order.action,
        "exchange": order.exchange,
        "sec_type": order.sec_type,
    }
    if order_type == "LIMIT":
        kwargs["price"] = order.price

    metadata: Dict[str, Any] = {
        "source": "api.place_order",
        "submitted_by": user or "anonymous",
    }
    if order.risk_rule:
        metadata["risk_rule"] = order.risk_rule.model_dump(exclude_none=True)

    payload = {
        "command_id": command_id,
        "method": method,
        "args": [],
        "kwargs": kwargs,
        "metadata": metadata,
    }

    await _ensure_ib_command_available()
    try:
        await _enqueue_command(payload)
    except Exception as exc:
        logger.error("Failed to enqueue IB command", exc_info=True)
        await audit_log_failure(
            request,
            detail="failed to enqueue order command",
            extra={"command_id": command_id, "symbol": order.symbol},
        )
        raise HTTPException(status_code=503, detail="Failed to enqueue order command") from exc

    await _broadcast_event(
        {
            "type": "order_command_enqueued",
            "command_id": command_id,
            "symbol": order.symbol,
            "action": order.action,
            "order_type": order_type,
        }
    )

    await audit_log_success(
        request,
        detail=f"queued order {command_id}",
        extra={
            "command_id": command_id,
            "symbol": order.symbol,
            "action": order.action,
            "order_type": order_type,
        },
    )

    return {
        "success": True,
        "command_id": command_id,
        "status": "queued",
    }

@router.delete("/api/cancel_order/{order_id}")
async def cancel_order(
    order_id: int,
    request: Request,
    user: Optional[str] = Depends(require_roles("admin", "operator", action="orders.cancel")),
):
    """取消订单"""
    if not connection_manager.is_connected():
        await audit_log_failure(
            request,
            detail="cancel attempted while disconnected",
            extra={"order_id": order_id},
        )
        raise HTTPException(status_code=400, detail="Not connected to IBKR")

    command_id = uuid.uuid4().hex
    payload = {
        "command_id": command_id,
        "method": "cancel_order",
        "args": [],
        "kwargs": {"order_id": order_id},
        "metadata": {
            "source": "api.cancel_order",
            "submitted_by": user or "anonymous",
        },
    }

    await _ensure_ib_command_available()
    try:
        await _enqueue_command(payload)
    except Exception as exc:
        logger.error("Failed to enqueue cancel order command", exc_info=True)
        await audit_log_failure(
            request,
            detail="failed to enqueue cancel command",
            extra={"order_id": order_id, "command_id": command_id},
        )
        raise HTTPException(status_code=503, detail="Failed to enqueue cancel command") from exc

    await _broadcast_event(
        {
            "type": "order_cancel_requested",
            "order_id": order_id,
            "command_id": command_id,
        }
    )

    await audit_log_success(
        request,
        detail=f"queued cancel for order {order_id}",
        extra={"order_id": order_id, "command_id": command_id},
    )

    return {
        "success": True,
        "status": "queued",
        "command_id": command_id,
        "order_id": order_id,
    }

@router.post("/api/cancel_order")
async def cancel_order_post(
    request: dict,
    http_request: Request,
    user: Optional[str] = Depends(require_roles("admin", "operator", action="orders.cancel")),
):
    """取消订单 (POST方法)"""
    order_id = request.get("order_id")
    if not order_id:
        await audit_log_failure(http_request, detail="order_id missing in cancel request")
        raise HTTPException(status_code=400, detail="order_id is required")

    try:
        order_id_int = int(order_id)
    except (TypeError, ValueError):
        await audit_log_failure(http_request, detail="invalid order_id in cancel request")
        raise HTTPException(status_code=400, detail="order_id must be an integer")

    return await cancel_order(order_id_int, http_request, user=user)

@router.post("/api/close_position")
async def close_position(
    request: dict,
    http_request: Request,
    user: Optional[str] = Depends(require_roles("admin", "operator", action="orders.close_position")),
):
    """平仓接口 - 根据持仓情况自动计算平仓订单"""
    symbol = request.get("symbol")
    if not symbol:
        await audit_log_failure(http_request, detail="symbol missing for close position")
        raise HTTPException(status_code=400, detail="Symbol is required")

    if not connection_manager.is_connected():
        await audit_log_failure(
            http_request,
            detail="close position attempted while disconnected",
            extra={"symbol": symbol},
        )
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    try:
        positions, snapshot = await _load_positions_snapshot()
        target_position: Optional[Dict[str, Any]] = None
        symbol_upper = str(symbol).upper()
        for pos in positions:
            pos_symbol = str(pos.get("symbol") or "").upper()
            if pos_symbol == symbol_upper:
                target_position = pos
                break

        if not target_position:
            await audit_log_failure(
                http_request,
                detail="no position found",
                extra={"symbol": symbol},
            )
            raise HTTPException(status_code=400, detail=f"No position found for symbol {symbol}")

        position_qty = _normalise_position_quantity(target_position.get("position"))
        if abs(position_qty) < 1e-9:
            await audit_log_failure(
                http_request,
                detail="position quantity zero",
                extra={"symbol": symbol},
            )
            raise HTTPException(status_code=400, detail=f"No position to close for symbol {symbol}")

        action = "SELL" if position_qty > 0 else "BUY"
        quantity = abs(int(position_qty))
        if quantity <= 0:
            await audit_log_failure(
                http_request,
                detail="calculated quantity invalid",
                extra={"symbol": symbol, "position_qty": position_qty},
            )
            raise HTTPException(status_code=400, detail="Unable to determine close quantity")

        exchange = target_position.get("exchange") or "SMART"
        sec_type = target_position.get("secType") or target_position.get("sec_type") or "STK"

        command_id = uuid.uuid4().hex
        payload = {
            "command_id": command_id,
            "method": "place_market_order",
            "args": [],
            "kwargs": {
                "symbol": symbol_upper,
                "quantity": quantity,
                "action": action,
                "exchange": exchange,
                "sec_type": sec_type,
            },
            "metadata": {
                "source": "api.close_position",
                "submitted_by": user or "anonymous",
                "original_position": position_qty,
                "snapshot_age": (snapshot or {}).get("cache_age_seconds"),
            },
        }

        await _ensure_ib_command_available()
        await _enqueue_command(payload)

        await _broadcast_event(
            {
                "type": "position_close_queued",
                "symbol": symbol_upper,
                "command_id": command_id,
                "action": action,
                "quantity": quantity,
            }
        )

        await audit_log_success(
            http_request,
            detail=f"queued close position for {symbol_upper}",
            extra={
                "symbol": symbol_upper,
                "command_id": command_id,
                "quantity": quantity,
                "action": action,
            },
        )

        return {
            "success": True,
            "status": "queued",
            "command_id": command_id,
            "symbol": symbol_upper,
            "action": action,
            "quantity": quantity,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Close position error: {e}")
        await audit_log_failure(
            http_request,
            detail=str(e),
            extra={"symbol": symbol},
        )
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/orders/validate")
async def validate_order(order: OrderRequest, user: Optional[str] = Depends(get_current_user)):
    """订单验证接口 - 执行 G 规则校验"""
    try:
        # 初始化验证结果
        validation_result = {
            "valid": True,
            "errors": [],
            "warnings": [],
            "g_rules_checked": [],
            "sizing_explain": None
        }
        
        # G7: 基本参数验证
        g7_result = _validate_g7_basic_params(order)
        validation_result["g_rules_checked"].append(g7_result)
        if not g7_result["passed"]:
            validation_result["valid"] = False
            validation_result["errors"].extend(g7_result["errors"])
        
        # G6: 连接状态验证
        g6_result = _validate_g6_connection()
        validation_result["g_rules_checked"].append(g6_result)
        if not g6_result["passed"]:
            validation_result["valid"] = False
            validation_result["errors"].extend(g6_result["errors"])
        
        # G5: 市场时间验证
        g5_result = _validate_g5_market_hours(order.symbol)
        validation_result["g_rules_checked"].append(g5_result)
        if not g5_result["passed"]:
            validation_result["warnings"].extend(g5_result["warnings"])
        
        # G4: 风险管理验证
        g4_result = await _validate_g4_risk_management(order)
        validation_result["g_rules_checked"].append(g4_result)
        if not g4_result["passed"]:
            validation_result["valid"] = False
            validation_result["errors"].extend(g4_result["errors"])
        
        # G3: 持仓限制验证
        g3_result = await _validate_g3_position_limits(order)
        validation_result["g_rules_checked"].append(g3_result)
        if not g3_result["passed"]:
            validation_result["valid"] = False
            validation_result["errors"].extend(g3_result["errors"])
        
        # G2: 资金充足性验证
        g2_result = await _validate_g2_capital_adequacy(order)
        validation_result["g_rules_checked"].append(g2_result)
        if not g2_result["passed"]:
            validation_result["valid"] = False
            validation_result["errors"].extend(g2_result["errors"])
        
        # G1: 最终合规性检查
        g1_result = _validate_g1_final_compliance(order)
        validation_result["g_rules_checked"].append(g1_result)
        if not g1_result["passed"]:
            validation_result["valid"] = False
            validation_result["errors"].extend(g1_result["errors"])
        
        return validation_result
        
    except Exception as e:
        logger.error(f"Order validation error: {e}")
        return {
            "valid": False,
            "errors": [f"Validation system error: {str(e)}"],
            "warnings": [],
            "g_rules_checked": [],
            "sizing_explain": None
        }

def _validate_g7_basic_params(order: OrderRequest) -> dict:
    """G7: 基本参数验证"""
    errors = []
    
    if not order.symbol or len(order.symbol.strip()) == 0:
        errors.append("Symbol is required")
    
    if not order.action or order.action.upper() not in ["BUY", "SELL"]:
        errors.append("Action must be BUY or SELL")
    
    if not order.quantity or order.quantity <= 0:
        errors.append("Quantity must be positive")
    
    if not order.order_type or order.order_type.upper() not in ["MARKET", "LIMIT"]:
        errors.append("Order type must be MARKET or LIMIT")
    
    if order.order_type and order.order_type.upper() == "LIMIT" and (not order.price or order.price <= 0):
        errors.append("Price is required for limit orders")
    
    return {
        "rule": "G7",
        "description": "Basic parameter validation",
        "passed": len(errors) == 0,
        "errors": errors
    }

def _validate_g6_connection() -> dict:
    """G6: 连接状态验证"""
    errors = []
    
    if not connection_manager.is_connected():
        errors.append("Not connected to IBKR")
    
    return {
        "rule": "G6",
        "description": "Connection status validation",
        "passed": len(errors) == 0,
        "errors": errors
    }

def _validate_g5_market_hours(symbol: str) -> dict:
    """G5: 市场时间验证"""
    warnings = []
    
    # 简化的市场时间检查 - 实际应用中需要更复杂的逻辑
    current_time = datetime.now(NY_TZ)
    current_hour = current_time.hour
    current_weekday = current_time.weekday()
    
    # 周末检查
    if current_weekday >= 5:  # 周六、周日
        warnings.append("Market is closed on weekends")
    
    # 简单的交易时间检查 (9:30 AM - 4:00 PM ET)
    elif current_hour < 9 or (current_hour == 9 and current_time.minute < 30) or current_hour >= 16:
        warnings.append("Outside regular trading hours")
    
    return {
        "rule": "G5",
        "description": "Market hours validation",
        "passed": True,  # 警告不阻止订单
        "warnings": warnings
    }

async def _validate_g4_risk_management(order: OrderRequest) -> dict:
    """G4: 风险管理验证"""
    errors = []

    try:
        # 检查订单金额是否超过限制（示例：单笔订单不超过 $100,000）
        if order.price and order.quantity:
            order_value = order.price * order.quantity
            if order_value > 100000:
                errors.append(f"Order value ${order_value:,.2f} exceeds maximum limit of $100,000")
        
    except Exception as e:
        errors.append(f"Risk management check failed: {str(e)}")
    
    return {
        "rule": "G4",
        "description": "Risk management validation",
        "passed": len(errors) == 0,
        "errors": errors
    }

async def _validate_g3_position_limits(order: OrderRequest) -> dict:
    """G3: 持仓限制验证"""
    errors = []

    try:
        if not connection_manager.is_connected():
            errors.append("Not connected to IBKR")
        else:
            positions, snapshot = await _load_positions_snapshot()
            current_position = 0.0

            for pos in positions:
                if str(pos.get('symbol', '')).upper() == (order.symbol or '').upper():
                    current_position = _normalise_position_quantity(pos.get('position'))
                    break

            if order.action and order.action.upper() == "BUY":
                new_position = current_position + (order.quantity or 0)
            else:
                new_position = current_position - (order.quantity or 0)

            if abs(new_position) > 10000:
                errors.append(
                    f"Position limit exceeded: new position would be {new_position} shares (limit: 10,000)"
                )
            if not positions and snapshot is None:
                errors.append("Position data unavailable")

    except Exception as e:
        errors.append(f"Position limit check failed: {str(e)}")

    return {
        "rule": "G3",
        "description": "Position limits validation",
        "passed": len(errors) == 0,
        "errors": errors
    }

async def _validate_g2_capital_adequacy(order: OrderRequest) -> dict:
    """G2: 资金充足性验证"""
    errors = []

    try:
        if not connection_manager.is_connected():
            errors.append("Not connected to IBKR")
        else:
            account_info, snapshot = await _load_account_snapshot()
            available_funds = _extract_available_funds(account_info)

            if available_funds is None and snapshot is None:
                errors.append("Account data unavailable")

            if (
                order.action
                and order.action.upper() == "BUY"
                and order.price
                and order.quantity
                and available_funds is not None
            ):
                required_funds = order.price * order.quantity

                if required_funds > available_funds:
                    errors.append(
                        f"Insufficient funds: required ${required_funds:,.2f}, available ${available_funds:,.2f}"
                    )

    except Exception as e:
        errors.append(f"Capital adequacy check failed: {str(e)}")

    return {
        "rule": "G2",
        "description": "Capital adequacy validation",
        "passed": len(errors) == 0,
        "errors": errors
    }

def _validate_g1_final_compliance(order: OrderRequest) -> dict:
    """G1: 最终合规性检查"""
    errors = []
    warnings = []
    
    # 最终的合规性检查
    # 例如：检查是否在禁止交易列表中
    prohibited_symbols = ["RESTRICTED1", "RESTRICTED2"]  # 示例
    if order.symbol.upper() in prohibited_symbols:
        errors.append(f"Symbol {order.symbol} is on the prohibited trading list")
    
    # 检查订单是否符合监管要求
    if order.quantity > 1000:
        warnings.append("Large order may require additional reporting")
    
    return {
        "rule": "G1",
        "description": "Final compliance validation",
        "passed": len(errors) == 0,
        "errors": errors,
        "warnings": warnings
    }

@router.post("/api/reverse_position")
async def reverse_position(request: dict, user: Optional[str] = Depends(get_current_user)):
    """反向下单接口 - 根据持仓情况自动计算反向订单"""
    symbol = request.get("symbol")
    if not symbol:
        raise HTTPException(status_code=400, detail="Symbol is required")

    if not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")

    try:
        positions, _ = await _load_positions_snapshot()
        target_position: Optional[Dict[str, Any]] = None
        symbol_upper = str(symbol).upper()
        for pos in positions:
            if str(pos.get('symbol', '')).upper() == symbol_upper:
                target_position = pos
                break

        if not target_position:
            raise HTTPException(status_code=400, detail=f"No position found for symbol {symbol}")

        position_qty = _normalise_position_quantity(target_position.get('position'))
        if abs(position_qty) < 1e-9:
            raise HTTPException(status_code=400, detail=f"No position to reverse for symbol {symbol}")

        action = "SELL" if position_qty > 0 else "BUY"
        quantity = abs(int(position_qty)) * 2
        if quantity <= 0:
            raise HTTPException(status_code=400, detail="Unable to determine reverse quantity")

        exchange = target_position.get('exchange') or "SMART"
        sec_type = target_position.get('secType') or target_position.get('sec_type') or "STK"

        command_id = uuid.uuid4().hex
        payload = {
            "command_id": command_id,
            "method": "place_market_order",
            "args": [],
            "kwargs": {
                "symbol": symbol_upper,
                "quantity": quantity,
                "action": action,
                "exchange": exchange,
                "sec_type": sec_type,
            },
            "metadata": {
                "source": "api.reverse_position",
                "submitted_by": user or "anonymous",
                "original_position": position_qty,
            },
        }

        await _ensure_ib_command_available()
        await _enqueue_command(payload)

        await _broadcast_event({
            "type": "position_reverse_queued",
            "symbol": symbol_upper,
            "command_id": command_id,
            "action": action,
            "quantity": quantity,
            "original_position": position_qty,
        })

        return {
            "success": True,
            "status": "queued",
            "command_id": command_id,
            "symbol": symbol_upper,
            "action": action,
            "quantity": quantity,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Reverse position error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/api/cancel_all_orders")
async def cancel_all_orders(
    request: Request,
    user: Optional[str] = Depends(require_roles("admin", "operator", action="orders.cancel_all")),
):
    """取消所有订单"""
    if not connection_manager.is_connected():
        await audit_log_failure(request, detail="cancel all attempted while disconnected")
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    command_id = uuid.uuid4().hex
    payload = {
        "command_id": command_id,
        "method": "cancel_all_orders",
        "args": [],
        "kwargs": {},
        "metadata": {
            "source": "api.cancel_all_orders",
            "submitted_by": user or "anonymous",
        },
    }

    await _ensure_ib_command_available()
    try:
        await _enqueue_command(payload)
    except Exception as exc:
        logger.error("Failed to enqueue cancel all orders command", exc_info=True)
        await audit_log_failure(request, detail="failed to enqueue cancel all command", extra={"command_id": command_id})
        raise HTTPException(status_code=503, detail="Failed to enqueue cancel all command") from exc

    await _broadcast_event(
        {
            "type": "all_orders_cancel_requested",
            "command_id": command_id,
        }
    )

    await audit_log_success(
        request,
        detail="queued cancel all orders",
        extra={"command_id": command_id},
    )

    return {
        "success": True,
        "status": "queued",
        "command_id": command_id,
    }

# 订单缓存，避免频繁查询数据库
orders_cache: Dict[Tuple[int, int, Optional[str]], Dict[str, Any]] = {}
orders_cache_timestamp: Dict[Tuple[int, int, Optional[str]], datetime] = {}
ORDERS_CACHE_TTL = 3  # 缓存有效期（秒）

@router.get("/api/orders")
async def get_orders(user: Optional[str] = Depends(get_current_user), limit: int = 100, use_cache: bool = True,
                     start_date: Optional[str] = None, page: int = 1):
    """获取订单列表（从数据库读取，包含历史订单）
    - limit: 每页返回的订单数量，默认100条
    - use_cache: 是否使用API级别缓存（默认 True）
    - start_date: 起始时间（ISO字符串）
    - page: 页码，从1开始
    """
    global orders_cache, orders_cache_timestamp

    try:
        order_manager = _get_order_manager()
        if order_manager is None:
            raise HTTPException(status_code=500, detail="Order manager not initialized")

        start_dt = None
        if start_date:
            try:
                start_dt = parse_local_datetime(start_date)
            except Exception as e:
                logger.warning(f"Invalid start_date '{start_date}': {e}")
                start_dt = None

        current_time = now_local()
        if limit <= 0:
            limit = 100
        page = max(1, page)
        offset = (page - 1) * limit if limit else 0
        cache_key = (limit, page, start_dt.isoformat() if start_dt else None)

        if use_cache and not start_dt:
            cached_payload = orders_cache.get(cache_key)
            cached_timestamp = orders_cache_timestamp.get(cache_key)
            if cached_payload and cached_timestamp:
                cache_age = (current_time - cached_timestamp).total_seconds()
                if cache_age < ORDERS_CACHE_TTL:
                    logger.debug(f"Using orders cache, age: {cache_age:.1f}s")
                    response = dict(cached_payload)
                    response["from_cache"] = True
                    return response

        loop = asyncio.get_event_loop()
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: order_manager.get_orders(
                        limit=limit,
                        start_date=start_dt,
                        offset=offset,
                        return_total=True,
                    )
                ),
                timeout=5.0
            )

            if isinstance(result, tuple):
                orders_list, total_count = result
            else:
                orders_list = result
                total_count = len(orders_list)

            converted_orders = []
            for o in orders_list:
                raw_type = (o.order_type or '').upper()
                if raw_type in ('MARKET', 'MKT'):
                    order_type = 'MKT'
                elif raw_type in ('LIMIT', 'LMT'):
                    order_type = 'LMT'
                else:
                    order_type = raw_type or 'MKT'

                price_val = 0.0
                try:
                    if o.price is not None:
                        price_val = float(o.price)
                except Exception:
                    price_val = 0.0

                trigger_type = None
                try:
                    if getattr(o, 'order_source', 'manual') == 'auto':
                        rule_id = getattr(o, 'rule_id', None)
                        symbol = getattr(o, 'symbol', None)
                        if rule_id and rule_id != symbol:
                            trigger_type = 'strategy'
                        else:
                            trigger_type = 'risk_rule'
                except Exception:
                    trigger_type = None

                converted_order = {
                    'order_id': o.order_id,
                    'symbol': o.symbol,
                    'action': o.action,
                    'quantity': o.quantity,
                    'order_type': order_type,
                    'limit_price': price_val if order_type == 'LMT' and price_val > 0 else None,
                    'avg_fill_price': (o.fill_price if o.fill_price is not None else (price_val if order_type == 'MKT' and price_val > 0 else 0)),
                    'status': o.status,
                    'filled': o.quantity if o.status == 'Filled' else 0,
                    'remaining': 0 if o.status == 'Filled' else o.quantity,
                    'submit_time': to_local_iso(getattr(o, 'created_at', None)),
                    'created_at': to_local_iso(getattr(o, 'created_at', None)),
                    'order_source': getattr(o, 'order_source', 'manual'),
                    'strategy_name': getattr(o, 'strategy_name', None),
                    'trigger_type': trigger_type,
                    'commission': getattr(o, 'commission', None),
                    'pnl': getattr(o, 'pnl', None)
                }
                converted_orders.append(converted_order)

            total_pages = max(1, (total_count + limit - 1) // limit) if limit else 1
            meta = {
                "limit": limit,
                "page_size": limit,
                "page": page,
                "total": total_count,
                "total_pages": total_pages,
                "has_prev": page > 1,
                "has_next": page < total_pages,
                "start_date": start_date,
                "generated_at": to_local_iso(now_local()),
                "source": "rest_api",
            }

            response_payload = {"success": True, "orders": converted_orders, "meta": meta, "from_cache": False}

            if use_cache and not start_dt:
                orders_cache[cache_key] = response_payload
                orders_cache_timestamp[cache_key] = current_time

            return response_payload

        except asyncio.TimeoutError:
            cached_payload = orders_cache.get(cache_key)
            cache_time = orders_cache_timestamp.get(cache_key)
            if cached_payload and cache_time:
                cache_age = (current_time - cache_time).total_seconds()
                logger.warning(
                    f"Orders request timed out, returning cached data (age: {cache_age:.1f}s)"
                    if cache_age is not None else
                    "Orders request timed out, returning cached data"
                )
                response = dict(cached_payload)
                response["from_cache"] = True
                response["cache_warning"] = "Request timed out, showing cached data"
                return response

            logger.warning("Orders request timed out and no cache available")
            return {"success": False, "error": "Orders request timed out"}

    except HTTPException:
        raise
    except Exception as e:
        cached_payload = orders_cache.get(cache_key)
        cache_time = orders_cache_timestamp.get(cache_key)
        if cached_payload and cache_time:
            cache_age = (current_time - cache_time).total_seconds()
            logger.error(f"Get orders error: {e}, returning cached data (age: {cache_age:.1f}s)")
            response = dict(cached_payload)
            response["from_cache"] = True
            response["cache_warning"] = "Error occurred, showing cached data"
            return response

        logger.error(f"Get orders error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/orders/{order_id}")
async def get_order_detail(order_id: str, user: Optional[str] = Depends(get_current_user)):
    """获取单个订单详情"""
    try:
        order_manager = _get_order_manager()
        if order_manager is None:
            raise HTTPException(status_code=500, detail="Order manager not initialized")

        o = order_manager.get_order(str(order_id))
        if not o:
            raise HTTPException(status_code=404, detail="Order not found")

        # 字段映射，与 /api/orders 保持一致，并补充更多详情字段
        raw_type = (o.order_type or '').upper()
        if raw_type in ('MARKET', 'MKT'):
            order_type = 'MKT'
        elif raw_type in ('LIMIT', 'LMT'):
            order_type = 'LMT'
        else:
            order_type = raw_type or 'MKT'

        price_val = 0.0
        try:
            if o.price is not None:
                price_val = float(o.price)
        except Exception:
            price_val = 0.0

        trigger_type = None
        try:
            if getattr(o, 'order_source', 'manual') == 'auto':
                rule_id = getattr(o, 'rule_id', None)
                symbol = getattr(o, 'symbol', None)
                if rule_id and rule_id != symbol:
                    trigger_type = 'strategy'
                else:
                    trigger_type = 'risk_rule'
        except Exception:
            trigger_type = None

        converted_order = {
            'order_id': o.order_id,
            'symbol': o.symbol,
            'action': o.action,
            'quantity': o.quantity,
            'order_type': order_type,
            'limit_price': price_val if order_type == 'LMT' and price_val > 0 else None,
            'avg_fill_price': (o.fill_price if o.fill_price is not None else (price_val if order_type == 'MKT' and price_val > 0 else 0)),
            'status': o.status,
            'filled': o.quantity if o.status == 'Filled' else 0,
            'remaining': 0 if o.status == 'Filled' else o.quantity,
            'submit_time': to_local_iso(getattr(o, 'created_at', None)),
            'order_source': getattr(o, 'order_source', 'manual'),
            'trigger_type': trigger_type,
            # 详情字段
            'price': price_val if price_val > 0 else None,
            'executed_at': to_local_iso(getattr(o, 'executed_at', None)),
            'updated_at': to_local_iso(getattr(o, 'updated_at', None)),
            'strategy_name': getattr(o, 'strategy_name', None),
            'rule_id': getattr(o, 'rule_id', None),
            'parent_order_id': getattr(o, 'parent_order_id', None),
            'exchange': getattr(o, 'exchange', 'SMART'),
            'sec_type': getattr(o, 'sec_type', 'STK'),
            'notes': getattr(o, 'notes', None),
            'commission': getattr(o, 'commission', None),
        }

        return {"success": True, "order": converted_order}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Get order detail error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/orders/sync")
async def sync_orders(user: Optional[str] = Depends(get_current_user)):
    """手动同步订单状态和价格字段从IB"""
    if not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")

    try:
        order_manager = _get_order_manager()
        if order_manager is None:
            raise HTTPException(status_code=500, detail="Order manager not initialized")

        command_id = uuid.uuid4().hex
        payload = {
            "command_id": command_id,
            "method": "sync_orders",
            "args": [],
            "kwargs": {},
            "metadata": {
                "source": "api.sync_orders",
                "submitted_by": user or "anonymous",
            },
        }

        await _enqueue_command(payload)

        return {"success": True, "status": "queued", "command_id": command_id}

    except Exception as e:
        logger.error(f"Sync orders error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/orders/{order_id}/sync")
async def sync_single_order(order_id: str, user: Optional[str] = Depends(get_current_user)):
    """手动同步指定订单的状态，然后返回该订单的最新详情"""
    if not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")

    try:
        order_manager = _get_order_manager()
        if order_manager is None:
            raise HTTPException(status_code=500, detail="Order manager not initialized")

        if not order_manager.get_order(str(order_id)):
            raise HTTPException(status_code=404, detail="Order not found")

        command_id = uuid.uuid4().hex
        payload = {
            "command_id": command_id,
            "method": "sync_order",
            "args": [],
            "kwargs": {"order_id": str(order_id)},
            "metadata": {
                "source": "api.sync_single_order",
                "submitted_by": user or "anonymous",
            },
        }

        await _enqueue_command(payload)

        return {
            "success": True,
            "status": "queued",
            "command_id": command_id,
            "order_id": str(order_id),
        }

    except Exception as e:
        logger.error(f"Sync single order error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

