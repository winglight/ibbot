import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional, Dict, Tuple, Any

from fastapi import APIRouter, HTTPException, Depends
from loguru import logger

import core
from core import connection_manager, websocket_manager

order_manager = getattr(core, "order_manager", None)
risk_manager = getattr(core, "risk_manager", None)
from .auth import get_current_user
from src.models import OrderRequest
from src.utils import ensure_local_timezone, now_local, parse_local_datetime, to_local_iso

router = APIRouter()

NY_TZ = ZoneInfo("America/New_York")


def _require_ib_client():
    """Return the active IBKR client or raise a consistent not-connected error."""
    ib_client = getattr(core, "ibkr_client", None)
    if ib_client is None:
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    return ib_client

@router.post("/api/place_order")
async def place_order(order: OrderRequest, user: Optional[str] = Depends(get_current_user)):
    """下单"""
    if not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")

    ib_client = _require_ib_client()

    try:
        # 直接调用下单方法，避免嵌套的事件循环调用
        if order.order_type.upper() == "MARKET":
            trade = await ib_client.place_market_order(
                symbol=order.symbol,
                quantity=order.quantity,
                action=order.action,
                exchange=order.exchange,
                sec_type=order.sec_type
            )
        elif order.order_type.upper() == "LIMIT":
            if not order.price:
                raise HTTPException(status_code=400, detail="Price required for limit order")
            trade = ib_client.place_limit_order(
                symbol=order.symbol,
                quantity=order.quantity,
                price=order.price,
                action=order.action,
                exchange=order.exchange,
                sec_type=order.sec_type
            )
        else:
            raise HTTPException(status_code=400, detail="Invalid order type")
        
        if trade:
            # 使用 OrderManager 记录订单
            try:
                if 'order_manager' in globals() and order_manager:
                    normalized_order_type = "MKT" if order.order_type.upper() == "MARKET" else "LMT"
                    
                    # 准备订单备注，包含风险规则信息
                    notes_parts = []
                    if user:
                        notes_parts.append(f"submitted_by={user}")
                    
                    # 如果是限价单且包含风险规则，预设风险规则信息到备注中
                    if (order.order_type.upper() == "LIMIT" and order.risk_rule and 
                        (order.risk_rule.stop_loss_price or order.risk_rule.take_profit_price)):
                        risk_info = []
                        if order.risk_rule.stop_loss_price:
                            risk_info.append(f"SL={order.risk_rule.stop_loss_price}")
                        if order.risk_rule.take_profit_price:
                            risk_info.append(f"TP={order.risk_rule.take_profit_price}")
                        notes_parts.append(f"pending_risk_rule:{','.join(risk_info)}")
                    
                    order_manager.create_order(
                        order_id=str(trade.order.orderId),
                        symbol=order.symbol,
                        action=order.action,
                        quantity=order.quantity,
                        order_type=normalized_order_type,
                        price=order.price if order.price else None,
                        order_source="manual",
                        strategy_name=None,
                        rule_id="manual",
                        parent_order_id=None,
                        exchange=order.exchange,
                        sec_type=order.sec_type,
                        notes="; ".join(notes_parts) if notes_parts else None
                    )
                else:
                    logger.warning("OrderManager is not initialized; order will not be recorded in OrderManager")
            except Exception as rec_err:
                logger.warning(f"Failed to record order in OrderManager: {rec_err}")
            
            # 失效订单缓存，避免3秒缓存导致新订单不可见
            try:
                global orders_cache, orders_cache_timestamp
                orders_cache = {}
                orders_cache_timestamp = {}
            except Exception:
                pass

            await websocket_manager.broadcast({  # 替换 manager
                "type": "order_placed",
                "order_id": trade.order.orderId,
                "symbol": order.symbol,
                "action": order.action,
                "quantity": order.quantity
            })

            # 触发前端订单列表的通用刷新逻辑
            await websocket_manager.broadcast({
                "type": "order_update",
                "reason": "order_placed",
                "symbol": order.symbol
            })
            
            return {
                "success": True,
                "order_id": trade.order.orderId,
                "message": "Order placed successfully"
            }
        else:
            raise HTTPException(status_code=400, detail="Failed to place order")
    except Exception as e:
        logger.error(f"Order placement error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/api/cancel_order/{order_id}")
async def cancel_order(order_id: int, user: Optional[str] = Depends(get_current_user)):
    """取消订单"""
    if not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")

    ib_client = _require_ib_client()

    try:
        # Run in executor to avoid blocking event loop with timeout
        loop = asyncio.get_event_loop()
        try:
            success, message = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: ib_client.cancel_order(order_id)
                ),
                timeout=35.0  # 35-second timeout for order cancellation
            )
        except asyncio.TimeoutError:
            logger.error(f"Order cancellation timed out for order {order_id}")
            return {"success": False, "error": "Order cancellation timed out"}

        if success:
            # 更新 OrderManager 本地状态
            try:
                if 'order_manager' in globals() and order_manager:
                    updated = order_manager.cancel_order(str(order_id), reason="Cancelled via API")
                    if not updated:
                        logger.warning(f"OrderManager could not update order {order_id} to Cancelled")
                else:
                    logger.warning("OrderManager is not initialized; local order state not updated")
            except Exception as rec_err:
                logger.warning(f"Failed to update OrderManager on cancel: {rec_err}")

            await websocket_manager.broadcast({  # 替换 manager
                "type": "order_cancelled",
                "order_id": order_id
            })

            return {"success": True, "message": message}
        else:
            # 订单未找到或状态不可取消
            raise HTTPException(status_code=404, detail=message)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Order cancellation error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/cancel_order")
async def cancel_order_post(request: dict, user: Optional[str] = Depends(get_current_user)):
    """取消订单 (POST方法)"""
    if not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")

    ib_client = _require_ib_client()

    try:
        order_id = request.get('order_id')
        if not order_id:
            raise HTTPException(status_code=400, detail="order_id is required")

        # Run in executor to avoid blocking event loop
        loop = asyncio.get_event_loop()
        try:
            success, message = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: ib_client.cancel_order(int(order_id))
                ),
                timeout=35.0  # 35-second timeout for order cancellation
            )
        except asyncio.TimeoutError:
            logger.error(f"Order cancellation timed out for order {order_id}")
            return {"success": False, "error": "Order cancellation timed out"}
        
        if success:
            # 更新 OrderManager 本地状态
            try:
                if 'order_manager' in globals() and order_manager:
                    updated = order_manager.cancel_order(str(order_id), reason="Cancelled via API")
                    if not updated:
                        logger.warning(f"OrderManager could not update order {order_id} to Cancelled")
                else:
                    logger.warning("OrderManager is not initialized; local order state not updated")
            except Exception as rec_err:
                logger.warning(f"Failed to update OrderManager on cancel: {rec_err}")
            
            await websocket_manager.broadcast({  # 替换 manager
                "type": "order_cancelled",
                "order_id": order_id
            })
            
            return {"success": True, "message": message}
        else:
            return {"success": False, "error": message}
            
    except Exception as e:
        logger.error(f"Order cancellation error: {e}")
        return {"success": False, "error": str(e)}

@router.post("/api/close_position")
async def close_position(request: dict, user: Optional[str] = Depends(get_current_user)):
    """平仓接口 - 根据持仓情况自动计算平仓订单"""
    symbol = request.get("symbol")
    if not symbol:
        raise HTTPException(status_code=400, detail="Symbol is required")
    
    if not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")

    ib_client = _require_ib_client()

    try:
        # 获取当前持仓。使用 fast 模式并设置超时，避免逐个订阅行情导致接口阻塞。
        try:
            positions = await asyncio.wait_for(
                ib_client.get_positions(fast=True),
                timeout=12.0,
            )
        except asyncio.TimeoutError:
            logger.error("Timed out while fetching positions for close_position")
            raise HTTPException(status_code=504, detail="Fetching positions timed out")
        target_position = None
        
        for pos in positions:
            if pos.get('symbol') == symbol:
                target_position = pos
                break
        
        if not target_position:
            raise HTTPException(status_code=400, detail=f"No position found for symbol {symbol}")
        
        position_qty = float(target_position.get('position', 0))
        if position_qty == 0:
            raise HTTPException(status_code=400, detail=f"No position to close for symbol {symbol}")
        
        # 计算平仓参数
        action = "SELL" if position_qty > 0 else "BUY"
        quantity = abs(int(position_qty))
        
        # 执行平仓市价单
        trade = await ib_client.place_market_order(
            symbol=symbol,
            quantity=quantity,
            action=action,
            exchange=target_position.get('exchange', 'SMART'),
            sec_type=target_position.get('secType', 'STK')
        )
        
        if trade:
            # 记录订单
            try:
                if 'order_manager' in globals() and order_manager:
                    order_manager.create_order(
                        order_id=str(trade.order.orderId),
                        symbol=symbol,
                        action=action,
                        quantity=quantity,
                        order_type="MKT",
                        price=None,
                        order_source="close_position",
                        strategy_name=None,
                        rule_id="close_position",
                        parent_order_id=None,
                        exchange=target_position.get('exchange', 'SMART'),
                        sec_type=target_position.get('secType', 'STK'),
                        notes=f"close_position_by={user}" if user else "close_position"
                    )
            except Exception as rec_err:
                logger.warning(f"Failed to record close position order: {rec_err}")
            
            await websocket_manager.broadcast({
                "type": "position_closed",
                "symbol": symbol,
                "order_id": trade.order.orderId,
                "action": action,
                "quantity": quantity
            })
            
            return {
                "success": True,
                "order_id": trade.order.orderId,
                "action": action,
                "quantity": quantity,
                "message": f"Position closed successfully for {symbol}"
            }
        else:
            raise HTTPException(status_code=400, detail="Failed to place close position order")
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Close position error: {e}")
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
    
    if not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")

    ib_client = _require_ib_client()

    try:
        # 获取当前持仓
        positions = await ib_client.get_positions()
        target_position = None
        
        for pos in positions:
            if pos.get('symbol') == symbol:
                target_position = pos
                break
        
        if not target_position:
            raise HTTPException(status_code=400, detail=f"No position found for symbol {symbol}")
        
        position_qty = float(target_position.get('position', 0))
        if position_qty == 0:
            raise HTTPException(status_code=400, detail=f"No position to reverse for symbol {symbol}")
        
        # 计算反向订单参数（按当前持仓量的2倍反向操作）
        action = "SELL" if position_qty > 0 else "BUY"
        quantity = abs(int(position_qty)) * 2  # 使用2倍持仓量进行反向下单
        
        # 在下单前反激活对应的风险规则
        try:
            if 'risk_manager' in globals() and risk_manager:
                if symbol in risk_manager.risk_rules:
                    rule = risk_manager.risk_rules[symbol]
                    if rule.enabled:
                        rule.enabled = False
                        
                        # 更新数据库中的风险规则状态
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
                        
                        # 发送通知
                        await websocket_manager.broadcast({
                            "type": "risk_rule_deactivated",
                            "symbol": symbol,
                            "message": f"反向下单前已自动停用风险规则: {symbol}"
                        })
        except Exception as e:
            logger.warning(f"反向下单前停用风险规则失败: {e}")
        
        # 执行反向市价单
        trade = await ib_client.place_market_order(
            symbol=symbol,
            quantity=quantity,
            action=action,
            exchange=target_position.get('exchange', 'SMART'),
            sec_type=target_position.get('secType', 'STK')
        )
        
        if trade:
            # 记录订单
            try:
                if 'order_manager' in globals() and order_manager:
                    order_manager.create_order(
                        order_id=str(trade.order.orderId),
                        symbol=symbol,
                        action=action,
                        quantity=quantity,
                        order_type="MKT",
                        price=None,
                        order_source="reverse_position",
                        strategy_name=None,
                        rule_id="reverse_position",
                        parent_order_id=None,
                        exchange=target_position.get('exchange', 'SMART'),
                        sec_type=target_position.get('secType', 'STK'),
                        notes=f"reverse_position_by={user}" if user else "reverse_position"
                    )
            except Exception as rec_err:
                logger.warning(f"Failed to record reverse position order: {rec_err}")
            
            await websocket_manager.broadcast({
                "type": "position_reversed",
                "symbol": symbol,
                "order_id": trade.order.orderId,
                "action": action,
                "quantity": quantity,
                "original_position": position_qty
            })
            
            return {
                "success": True,
                "order_id": trade.order.orderId,
                "action": action,
                "quantity": quantity,
                "original_position": position_qty,
                "message": f"Position reversed successfully for {symbol}"
            }
        else:
            raise HTTPException(status_code=400, detail="Failed to place reverse position order")
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Reverse position error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/api/cancel_all_orders")
async def cancel_all_orders(user: Optional[str] = Depends(get_current_user)):
    """取消所有订单"""
    if not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")

    ib_client = _require_ib_client()

    try:
        # 直接调用取消所有订单方法，避免嵌套的事件循环调用
        cancelled_count = ib_client.cancel_all_orders()
        
        await websocket_manager.broadcast({  # 替换 manager
            "type": "all_orders_cancelled",
            "count": cancelled_count
        })
        
        return {
            "success": True,
            "cancelled_count": cancelled_count,
            "message": f"Cancelled {cancelled_count} orders"
        }
        
    except Exception as e:
        logger.error(f"Cancel all orders error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

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
