import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional, Dict, Tuple, Any

from fastapi import APIRouter, HTTPException, Depends
from loguru import logger

import core
from core import get_core_container

order_manager = getattr(core, "order_manager", None)
risk_manager = getattr(core, "risk_manager", None)
from .auth import get_current_user
from src.models import OrderRequest
from src.utils import ensure_local_timezone, now_local, parse_local_datetime, to_local_iso
from ibbot.redis import enqueue_ib_command, wait_for_command_result
from src.ib_command_service import (
    IBCommandServiceConfig,
    CommandServiceStatus,
    command_wait_timeout,
    ensure_ib_command_redis,
    get_ib_command_service_status,
    load_ib_command_config,
)

core_container = get_core_container()
connection_manager = core_container.connection_manager
websocket_manager = core_container.websocket_manager
router = APIRouter()

NY_TZ = ZoneInfo("America/New_York")


_IB_COMMAND_CONFIG: IBCommandServiceConfig = load_ib_command_config()
_IB_COMMAND_WAIT_TIMEOUT: float = command_wait_timeout(_IB_COMMAND_CONFIG)


async def _prepare_command_service(
    *, require_connected: bool = True
) -> Tuple[Any, CommandServiceStatus]:
    """Return the Redis client and latest service status."""

    status = await get_ib_command_service_status(config=_IB_COMMAND_CONFIG)
    if require_connected and not status.connected:
        detail = status.error or "IB command service unavailable"
        raise HTTPException(status_code=503, detail=detail)

    redis = await ensure_ib_command_redis(_IB_COMMAND_CONFIG)
    if redis is None:
        raise HTTPException(status_code=503, detail="Redis unavailable")

    return redis, status


def _command_response(
    *,
    command: str,
    command_id: str,
    status: CommandServiceStatus,
    result: Optional[Dict[str, Any]] = None,
    waited: bool = False,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "success": True,
        "status": "queued" if result is None else str(result.get("status", "ok")).lower(),
        "command": command,
        "command_id": command_id,
        "waited": waited,
    }
    if status.health is not None:
        payload["service_health"] = status.health
    if waited:
        payload["waited"] = True
        payload["timeout"] = timeout
    if result is not None:
        payload["result"] = result
        if payload["status"] in {"error", "failed"}:
            payload["success"] = False
    return payload


async def _submit_command(
    command: str,
    payload: Dict[str, Any],
    *,
    user: Optional[str],
    wait: bool = False,
    timeout: Optional[float] = None,
    broadcast_extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    redis, status = await _prepare_command_service()

    command_payload = dict(payload)
    if user:
        command_payload.setdefault("meta", {})
        command_payload["meta"]["submitted_by"] = user

    command_id = await enqueue_ib_command(
        redis,
        command,
        command_payload,
        queue_key=_IB_COMMAND_CONFIG.command_queue_key,
        result_prefix=_IB_COMMAND_CONFIG.command_result_prefix,
        result_ttl=_IB_COMMAND_CONFIG.command_result_ttl_seconds,
        source=_IB_COMMAND_CONFIG.service_name,
    )

    if not command_id:
        raise HTTPException(status_code=503, detail=f"Failed to enqueue {command} command")

    event: Dict[str, Any] = {
        "type": "order_command_submitted",
        "command": command,
        "command_id": command_id,
    }
    if broadcast_extra:
        event.update(broadcast_extra)
    if user:
        event["submitted_by"] = user

    try:
        await websocket_manager.broadcast(event)
    except Exception:  # pragma: no cover - best effort notification
        logger.debug("Failed to broadcast order command submission", command=command)

    wait_timeout = timeout if timeout is not None else _IB_COMMAND_WAIT_TIMEOUT
    result: Optional[Dict[str, Any]] = None
    if wait:
        result = await wait_for_command_result(
            redis,
            command_id,
            result_prefix=_IB_COMMAND_CONFIG.command_result_prefix,
            timeout=wait_timeout,
        )

    response = _command_response(
        command=command,
        command_id=command_id,
        status=status,
        result=result if result is not None else None,
        waited=wait,
        timeout=wait_timeout if wait else None,
    )

    if wait and result is None:
        response["status"] = "timeout"
        response["success"] = False
        response["error"] = "Command result not available"

    response.setdefault("timeout", wait_timeout)
    return response


def _require_ib_client():
    """Return the active IBKR client or raise a consistent not-connected error."""
    ib_client = getattr(core, "ibkr_client", None)
    if ib_client is None:
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    return ib_client

@router.post("/api/place_order")
async def place_order(order: OrderRequest, user: Optional[str] = Depends(get_current_user)):
    """下单"""
    order_payload = order.model_dump(exclude_none=True)
    order_payload["action"] = order_payload.get("action", "").upper()
    order_payload["order_type"] = order_payload.get("order_type", "").upper()
    order_payload.setdefault("exchange", order.exchange)
    order_payload.setdefault("sec_type", order.sec_type)
    order_payload.setdefault("quantity", order.quantity)
    order_payload.setdefault("meta", {})
    order_payload["meta"].setdefault("source", "manual")

    # Drop local caches to ensure a subsequent refresh fetches from upstream
    try:
        global orders_cache, orders_cache_timestamp
        orders_cache = {}
        orders_cache_timestamp = {}
    except Exception:  # pragma: no cover - cache invalidation best effort
        pass

    return await _submit_command(
        "place_order",
        order_payload,
        user=user,
        wait=False,
        broadcast_extra={
            "symbol": order.symbol,
            "action": order_payload["action"],
            "quantity": order.quantity,
        },
    )

@router.delete("/api/cancel_order/{order_id}")
async def cancel_order(
    order_id: int,
    user: Optional[str] = Depends(get_current_user),
    wait: bool = False,
    timeout: Optional[float] = None,
):
    """取消订单"""

    response = await _submit_command(
        "cancel_order",
        {"order_id": str(order_id)},
        user=user,
        wait=wait,
        timeout=timeout,
        broadcast_extra={"order_id": order_id},
    )

    if wait and response.get("status") == "timeout":
        response.setdefault("error", "Cancellation result not available")
    return response

@router.post("/api/cancel_order")
async def cancel_order_post(
    request: dict,
    user: Optional[str] = Depends(get_current_user),
    wait: bool = False,
    timeout: Optional[float] = None,
):
    """取消订单 (POST方法)"""
    order_id = request.get("order_id")
    if not order_id:
        raise HTTPException(status_code=400, detail="order_id is required")

    response = await _submit_command(
        "cancel_order",
        {"order_id": str(order_id)},
        user=user,
        wait=wait,
        timeout=timeout,
        broadcast_extra={"order_id": order_id},
    )

    if wait and response.get("status") == "timeout":
        response.setdefault("error", "Cancellation result not available")
    return response

@router.post("/api/close_position")
async def close_position(request: dict, user: Optional[str] = Depends(get_current_user)):
    """平仓接口 - 根据持仓情况自动计算平仓订单"""
    symbol = request.get("symbol")
    if not symbol:
        raise HTTPException(status_code=400, detail="Symbol is required")

    payload: Dict[str, Any] = {"symbol": symbol}
    for field in ("exchange", "sec_type", "quantity", "order_type"):
        if field in request and request.get(field) is not None:
            payload[field] = request.get(field)

    response = await _submit_command(
        "close_position",
        payload,
        user=user,
        wait=False,
        broadcast_extra={"symbol": symbol},
    )

    response.setdefault("message", f"Close position command queued for {symbol}")
    return response

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

        command_status = await get_ib_command_service_status(
            config=_IB_COMMAND_CONFIG
        )
        
        # G7: 基本参数验证
        g7_result = _validate_g7_basic_params(order)
        validation_result["g_rules_checked"].append(g7_result)
        if not g7_result["passed"]:
            validation_result["valid"] = False
            validation_result["errors"].extend(g7_result["errors"])
        
        # G6: 连接状态验证
        g6_result = await _validate_g6_connection(command_status)
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

async def _validate_g6_connection(
    status: Optional[CommandServiceStatus] = None,
) -> dict:
    """G6: 连接状态验证"""

    errors: list[str] = []

    try:
        if status is None:
            status = await get_ib_command_service_status(config=_IB_COMMAND_CONFIG)
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.warning(f"Failed to resolve IB command service status: {exc}")
        status = None

    connected = bool(status.connected) if status else False
    if not connected:
        message = status.error if status else None
        errors.append(message or "IB command service unavailable")

    return {
        "rule": "G6",
        "description": "Connection status validation",
        "passed": len(errors) == 0,
        "errors": errors,
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
        # 检查是否有活跃的风险规则
        if risk_manager:
            # 这里可以添加具体的风险规则检查
            # 例如：检查单笔订单金额限制、日内交易次数限制等
            pass
        
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
        if connection_manager.is_connected():
            ib_client = getattr(core, "ibkr_client", None)
            if ib_client is None:
                errors.append("Not connected to IBKR")
            else:
                # 获取当前持仓
                positions = await ib_client.get_positions()
                current_position = 0

                for pos in positions:
                    if pos.get('symbol') == order.symbol:
                        current_position = float(pos.get('position', 0))
                        break

                # 计算订单执行后的持仓
                if order.action.upper() == "BUY":
                    new_position = current_position + order.quantity
                else:
                    new_position = current_position - order.quantity

                # 检查持仓限制（示例：单个股票持仓不超过 10,000 股）
                if abs(new_position) > 10000:
                    errors.append(f"Position limit exceeded: new position would be {new_position} shares (limit: 10,000)")

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
        if connection_manager.is_connected():
            ib_client = getattr(core, "ibkr_client", None)
            if ib_client is None:
                errors.append("Not connected to IBKR")
            else:
                # 获取账户信息
                account_info = await ib_client.get_account_summary()

                if account_info:
                    # 检查可用资金
                    available_funds = 0
                    for item in account_info:
                        if item.get('tag') == 'AvailableFunds':
                            available_funds = float(item.get('value', 0))
                            break

                    # 计算订单所需资金
                    if order.action.upper() == "BUY" and order.price and order.quantity:
                        required_funds = order.price * order.quantity

                        if required_funds > available_funds:
                            errors.append(f"Insufficient funds: required ${required_funds:,.2f}, available ${available_funds:,.2f}")

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

    # 在下单前反激活对应的风险规则
    try:
        if 'risk_manager' in globals() and risk_manager:
            if symbol in risk_manager.risk_rules:
                rule = risk_manager.risk_rules[symbol]
                if rule.enabled:
                    rule.enabled = False

                    from src.database import DatabaseRiskRule

                    db_rule = DatabaseRiskRule(
                        symbol=rule.symbol,
                        stop_loss_price=rule.stop_loss_price,
                        take_profit_price=rule.take_profit_price,
                        max_loss_percent=rule.max_loss_percent,
                        enabled=rule.enabled,
                        max_time_span=rule.max_time_span,
                        timestamp=to_local_iso(rule.created_at) or now_local().isoformat(),
                        auto_trailing=rule.auto_trailing,
                        atr_params=rule.atr_params,
                        rule_type=rule.rule_type,
                    )
                    risk_manager.db_manager.save_risk_rule(db_rule)

                    logger.info(f"🔒 反向下单前已停用风险规则: {symbol}")

                    await websocket_manager.broadcast({
                        "type": "risk_rule_deactivated",
                        "symbol": symbol,
                        "message": f"反向下单前已自动停用风险规则: {symbol}"
                    })
    except Exception as e:
        logger.warning(f"反向下单前停用风险规则失败: {e}")

    payload: Dict[str, Any] = {"symbol": symbol}
    multiplier = request.get("multiplier", 2)
    if multiplier is not None:
        payload["multiplier"] = multiplier
    if request.get("force_quantity") is not None:
        payload["force_quantity"] = request.get("force_quantity")

    response = await _submit_command(
        "reverse_position",
        payload,
        user=user,
        wait=False,
        broadcast_extra={"symbol": symbol, "multiplier": payload.get("multiplier")},
    )

    response.setdefault("message", f"Reverse position command queued for {symbol}")
    return response

@router.delete("/api/cancel_all_orders")
async def cancel_all_orders(user: Optional[str] = Depends(get_current_user)):
    """取消所有订单"""
    response = await _submit_command(
        "cancel_all_orders",
        {},
        user=user,
        wait=False,
        broadcast_extra={},
    )

    response.setdefault("message", "Cancel all orders command queued")
    return response

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
        if 'order_manager' not in globals() or not order_manager:
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
        if 'order_manager' not in globals() or not order_manager:
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

    ib_client = _require_ib_client()

    try:
        if 'order_manager' not in globals() or not order_manager:
            raise HTTPException(status_code=500, detail="Order manager not initialized")
        
        # 手动同步订单状态
        loop = asyncio.get_event_loop()
        await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: ib_client.get_orders(risk_manager, getattr(core, 'strategy_manager', None), order_manager)
            ),
            timeout=10.0
        )
        
        return {"success": True, "message": "Orders synced successfully"}
        
    except asyncio.TimeoutError:
        logger.warning("Order sync request timed out")
        raise HTTPException(status_code=408, detail="Order sync request timed out")
    except Exception as e:
        logger.error(f"Sync orders error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/orders/{order_id}/sync")
async def sync_single_order(order_id: str, user: Optional[str] = Depends(get_current_user)):
    """手动同步指定订单的状态，然后返回该订单的最新详情"""
    if not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")

    ib_client = _require_ib_client()

    try:
        if 'order_manager' not in globals() or not order_manager:
            raise HTTPException(status_code=500, detail="Order manager not initialized")

        # 检查订单是否存在
        if not order_manager.get_order(str(order_id)):
            raise HTTPException(status_code=404, detail="Order not found")

        # 同步订单（IB接口暂不支持单个订单刷新，这里触发整体刷新）
        loop = asyncio.get_event_loop()
        await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: ib_client.get_orders(risk_manager, getattr(core, 'strategy_manager', None), order_manager)
            ),
            timeout=10.0
        )

        # 返回最新订单详情（复用现有的详情接口）
        return await get_order_detail(order_id, user)

    except asyncio.TimeoutError:
        logger.warning("Order sync request timed out")
        raise HTTPException(status_code=408, detail="Order sync request timed out")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Sync single order error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
