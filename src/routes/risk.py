import os
import asyncio
import json
import time
from dataclasses import asdict
from typing import Optional, Dict, Any
from datetime import datetime
from copy import deepcopy

from fastapi import APIRouter, HTTPException, Depends
from loguru import logger

import core
from core import connection_manager, risk_manager
from .auth import get_current_user
from src.models import (
    RiskRuleRequest,
    RiskConfigUpdateRequest,
    PositionSizingRequest,
)
from src.database import get_db_manager

from .orders import (
    _validate_g7_basic_params,
    _validate_g6_connection,
    _validate_g5_market_hours,
    _validate_g4_risk_management,
    _validate_g3_position_limits,
    _validate_g2_capital_adequacy,
    _validate_g1_final_compliance,
)

router = APIRouter()

# Cache the most recent successful risk configuration so that the endpoint can
# respond immediately even if the underlying manager becomes unresponsive.
_last_risk_config: Dict[str, Any] = {}
_risk_config_lock: asyncio.Lock = asyncio.Lock()
_last_risk_config_updated_at: float = 0.0
_last_risk_config_attempt_at: float = 0.0
_RISK_CONFIG_REFRESH_INTERVAL: float = 30.0


async def _refresh_risk_config(timeout: float) -> Optional[Dict[str, Any]]:
    """Fetch the latest risk configuration without blocking the request path."""

    global _last_risk_config, _last_risk_config_updated_at, _last_risk_config_attempt_at

    if 'risk_manager' not in globals() or risk_manager is None:
        return None

    async with _risk_config_lock:
        _last_risk_config_attempt_at = time.monotonic()
        try:
            config = await asyncio.wait_for(
                asyncio.to_thread(risk_manager.get_config),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("refresh_risk_config: fetching config timed out")
            return None
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.warning(f"refresh_risk_config: failed to fetch config: {exc}")
            return None

        _last_risk_config = deepcopy(config)
        _last_risk_config_updated_at = _last_risk_config_attempt_at
        return deepcopy(config)

@router.post("/api/risk_rules")
async def add_risk_rule(rule: RiskRuleRequest, user: Optional[str] = Depends(get_current_user)):
    """添加风险管理规则"""
    if not risk_manager:
        raise HTTPException(status_code=400, detail="Risk manager not initialized")
    
    try:
        logger.info(
            f"API add_risk_rule: symbol={rule.symbol}, SL={rule.stop_loss_price}, TP={rule.take_profit_price}, "
            f"max_loss%={rule.max_loss_percent}, max_time_span={rule.max_time_span}, rule_type={rule.rule_type}"
        )

        async_task = asyncio.create_task(
            asyncio.to_thread(
                risk_manager.add_risk_rule,
                rule.symbol,
                rule.stop_loss_price,
                rule.take_profit_price,
                rule.max_loss_percent,
                rule.max_time_span,
                rule.rule_type == 'trailing',
                rule.atr_params,
                rule.rule_type,
            )
        )

        try:
            success = await asyncio.wait_for(async_task, timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning(
                "add_risk_rule processing timed out; continuing in background"
            )

            def _log_task_result(task: asyncio.Task):
                try:
                    task.result()
                except Exception as task_err:  # pragma: no cover - best effort logging
                    logger.error(f"add_risk_rule background task failed: {task_err}")

            async_task.add_done_callback(_log_task_result)
            return {
                "success": True,
                "message": "Risk rule save is processing in background",
                "pending": True,
            }

        if not success:
            raise HTTPException(status_code=500, detail="Failed to add risk rule")

        return {"success": True, "message": "Risk rule added successfully"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Add risk rule error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/risk_rules")
async def get_risk_rules(user: Optional[str] = Depends(get_current_user)):
    """获取风险管理规则"""
    if not risk_manager:
        raise HTTPException(status_code=400, detail="Risk manager not initialized")
    
    try:
        loop = asyncio.get_event_loop()
        try:
            rules = await asyncio.wait_for(
                loop.run_in_executor(None, risk_manager.get_risk_rules),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            logger.warning("get_risk_rules timed out; returning cached state")
            rules = risk_manager.get_risk_rules()
        # Convert to serializable format
        serializable_rules = {}
        for symbol, rule in rules.items():
            serializable_rules[symbol] = {
                "symbol": rule.symbol,
                "stop_loss_price": rule.stop_loss_price,
                "take_profit_price": rule.take_profit_price,
                "max_loss_percent": rule.max_loss_percent,
                "max_time_span": rule.max_time_span,
                "enabled": rule.enabled,
                "created_at": rule.created_at.isoformat(),
                "rule_type": rule.rule_type,
                "auto_trailing": rule.auto_trailing,
                "atr_params": rule.atr_params or {},
            }
        
        return {"success": True, "rules": serializable_rules}
        
    except Exception as e:
        logger.error(f"Get risk rules error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.put("/api/risk_rules/{symbol}")
async def update_risk_rule(symbol: str, enabled: bool, user: Optional[str] = Depends(get_current_user)):
    """更新风险管理规则状态"""
    if not risk_manager:
        raise HTTPException(status_code=400, detail="Risk manager not initialized")
    
    try:
        logger.info(f"API update_risk_rule: symbol={symbol}, enabled={enabled}")
        success = risk_manager.update_risk_rule(symbol, enabled=enabled)
        if not success:
            # 让404错误向外传播
            raise HTTPException(status_code=404, detail=f"Risk rule for {symbol} not found")
        
        return {"success": True, "message": f"Risk rule for {symbol} {'enabled' if enabled else 'disabled'} successfully"}
        
    except HTTPException as he:
        # 直接抛出由业务逻辑产生的HTTP错误（如404）
        raise he
    except Exception as e:
        logger.error(f"Update risk rule error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/api/risk_rules/{symbol}")
async def remove_risk_rule(symbol: str, user: Optional[str] = Depends(get_current_user)):
    """移除风险管理规则"""
    if not risk_manager:
        raise HTTPException(status_code=400, detail="Risk manager not initialized")
    
    try:
        logger.info(f"API remove_risk_rule: symbol={symbol}")
        risk_manager.remove_risk_rule(symbol)
        return {"success": True, "message": "Risk rule removed successfully"}
        
    except Exception as e:
        logger.error(f"Remove risk rule error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/order_queue_status")
async def get_order_queue_status(user: Optional[str] = Depends(get_current_user)):
    """获取订单队列状态"""
    if not risk_manager:
        raise HTTPException(status_code=400, detail="Risk manager not initialized")
    
    try:
        queue_size = risk_manager.get_order_queue_size()
        return {
            "success": True,
            "queue_size": queue_size,
            "processing": getattr(risk_manager, "processing_orders", False)
        }
        
    except Exception as e:
        logger.error(f"Get order queue status error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/risk-config")
async def get_risk_config(user: Optional[str] = Depends(get_current_user)):
    """Get risk management configuration from environment variables"""
    try:
        stop_loss_ratio = float(os.getenv('STOP_LOSS_RATIO', '0.05'))
        take_profit_ratio = float(os.getenv('TAKE_PROFIT_RATIO', '0.05'))
        
        leverage = {}
        try:
            if 'risk_manager' in globals() and risk_manager is not None:
                lev_map = getattr(risk_manager, 'leverage_map', None)
                if isinstance(lev_map, dict):
                    leverage = {k: float(v) for k, v in lev_map.items()}
            if not leverage:
                default_mnq_lev = float(os.getenv('LEVERAGE_MNQ', 20))
                leverage = {'MNQ': default_mnq_lev}
        except Exception:
            leverage = {'MNQ': 20.0}

        # 新增：返回完整风控配置，添加超时保护并使用缓存避免阻塞
        global _last_risk_config, _last_risk_config_attempt_at, _last_risk_config_updated_at

        now = time.monotonic()
        cached_snapshot = deepcopy(_last_risk_config)
        if not cached_snapshot:
            # 没有可用缓存时尝试快速获取一次
            if (now - _last_risk_config_attempt_at > 1.0) and not _risk_config_lock.locked():
                refreshed = await _refresh_risk_config(timeout=0.5)
                if refreshed:
                    cached_snapshot = deepcopy(refreshed)
        else:
            should_refresh = (
                ('risk_manager' in globals() and risk_manager is not None)
                and (now - _last_risk_config_updated_at > _RISK_CONFIG_REFRESH_INTERVAL)
                and (now - _last_risk_config_attempt_at > 1.0)
                and not _risk_config_lock.locked()
            )
            if should_refresh:
                asyncio.create_task(_refresh_risk_config(timeout=1.5))

        full_cfg = cached_snapshot

        return {
            "stop_loss_ratio": stop_loss_ratio,
            "take_profit_ratio": take_profit_ratio,
            "leverage": leverage,
            "risk_config": full_cfg,
        }
    except Exception as e:
        logger.error(f"Get risk config error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# 新增：更新风控配置
@router.put("/api/risk-config")
async def update_risk_config(payload: RiskConfigUpdateRequest, user: Optional[str] = Depends(get_current_user)):
    if 'risk_manager' not in globals() or risk_manager is None:
        raise HTTPException(status_code=400, detail="Risk manager not initialized")
    try:
        updates = {k: v for k, v in payload.dict(exclude_none=True).items()}
        new_cfg = risk_manager.update_config(updates)
        global _last_risk_config, _last_risk_config_attempt_at, _last_risk_config_updated_at
        _last_risk_config = deepcopy(new_cfg)
        now = time.monotonic()
        _last_risk_config_attempt_at = now
        _last_risk_config_updated_at = now
        return {"success": True, "risk_config": new_cfg}
    except Exception as e:
        logger.error(f"Update risk config error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# 新增：仓位建议计算
# Removed duplicate /api/position-sizing endpoint - use /api/sizing/preview instead


@router.post("/api/position-sizing")
async def api_position_sizing(payload: PositionSizingRequest, user: Optional[str] = Depends(get_current_user)):
    """Position sizing calculation - optimized to only require symbol and stop_distance"""
    if 'risk_manager' not in globals() or risk_manager is None:
        raise HTTPException(status_code=400, detail="Risk manager not initialized")
    
    try:
        # 获取实时账户信息
        account_info = {}
        if connection_manager.is_connected():
            try:
                account_response = await asyncio.wait_for(
                    core.ibkr_client.get_account_info(),
                    timeout=5.0
                )
                if account_response:
                    account_info = account_response
            except Exception as e:
                logger.warning(f"Failed to get account info for position sizing: {e}")
        
        # 更新风险管理器的账户状态
        if account_info:
            risk_manager.update_equity_state(account_info)
        
        # 使用异步执行器和超时来避免阻塞
        # 只传递必需的参数，其他参数使用默认值或让risk_manager自动估算
        loop = asyncio.get_event_loop()
        try:
            res = await risk_manager.position_sizing(
                symbol=payload.symbol,
                stop_distance=payload.stop_distance,
                rv_current=None,  # 让risk_manager自动估算当前价格
                signal_tier='A',  # 使用默认信号等级
                execution_metrics=None,  # 使用默认执行指标
                rv_ref=None,  # 使用默认参考价格
            )
        except asyncio.TimeoutError:
            logger.warning(f"Position sizing request timed out for {payload.symbol}")
            raise HTTPException(status_code=408, detail="Position sizing calculation timed out. This may be due to slow market data retrieval.")
        
        # 执行G Rules验证（用于历史记录）
        g_rules_checked = []
        try:
            # 创建模拟订单对象进行验证
            from src.models import OrderRequest
            mock_order = OrderRequest(
                symbol=payload.symbol,
                action="BUY",  # 默认买入
                quantity=int(res.contracts),
                order_type="MKT",
                price=payload.rv_current
            )
            
            # 执行各项G Rules验证
            g7_result = _validate_g7_basic_params(mock_order)
            g_rules_checked.append(g7_result)
            
            g6_result = _validate_g6_connection()
            g_rules_checked.append(g6_result)
            
            g5_result = _validate_g5_market_hours(payload.symbol)
            g_rules_checked.append(g5_result)
            
            g4_result = await _validate_g4_risk_management(mock_order)
            g_rules_checked.append(g4_result)
            
            g3_result = await _validate_g3_position_limits(mock_order)
            g_rules_checked.append(g3_result)
            
            g2_result = await _validate_g2_capital_adequacy(mock_order)
            g_rules_checked.append(g2_result)
            
            g1_result = _validate_g1_final_compliance(mock_order)
            g_rules_checked.append(g1_result)
            
        except Exception as ve:
            logger.warning(f"G Rules validation failed: {ve}")
            g_rules_checked = []
        
        # 写入历史
        try:
            db = get_db_manager()
            db.save_sizing_history(
                symbol=payload.symbol,
                action="calculate",
                suggested_quantity=int(res.contracts),
                current_price=getattr(payload, 'rv_current', None),
                sizing_method="risk_based",
                calculation_details=json.dumps({
                    "request": payload.dict(exclude_none=True),
                    "result": asdict(res)
                }),
                g_rules_checked=json.dumps(g_rules_checked) if g_rules_checked else None
            )
        except Exception as he:
            logger.warning(f"/api/position-sizing: save history failed: {he}")
        
        # 返回完整的数据结构
        return {
            "N": int(res.contracts),
            "explain": res.explanation,
            "stopDistance": res.stop_distance,
            "pointValue": res.point_value,
            "scaleTotal": res.scale_total,
            "scaleSig": res.scale_sig,
            "scaleVol": res.scale_vol,
            "scaleDd": res.scale_dd,
            "scaleExec": res.scale_exec,
            "estimatedInvestment": res.R_star,
            "maxRisk": res.R,
            "calculationMethod": "Risk-based position sizing"
        }
    except Exception as e:
        logger.error(f"Position sizing error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# 新增：S 规则 - 历史查询
@router.get("/api/sizing/history")
async def get_sizing_history(symbol: Optional[str] = None, since: Optional[str] = None, limit: int = 100, user: Optional[str] = Depends(get_current_user)):
    try:
        db = get_db_manager()
        items = db.load_sizing_history(symbol=symbol, since=since, limit=limit)
        
        # 为每个历史记录添加g_rules_status和s_rules_status
        for item in items:
            # 解析G Rules状态
            g_rules_checked = item.get('g_rules_checked', [])
            g_rules_status = {}
            
            # 映射G1-G7规则到g0-g6（前端从g0开始）
            if isinstance(g_rules_checked, list):
                for i, rule_result in enumerate(g_rules_checked):
                    if isinstance(rule_result, dict):
                        rule_key = f"g{i}"  # g0, g1, g2...
                        g_rules_status[rule_key] = {
                            "passed": rule_result.get("passed", True),
                            "message": " | ".join(rule_result.get("errors", [])) if not rule_result.get("passed", True) else "正常"
                        }
            
            # 如果没有G规则数据，提供默认状态
            if not g_rules_status:
                for i in range(6):  # G0-G5
                    g_rules_status[f"g{i}"] = {"passed": True, "message": "正常"}
            
            # 生成S Rules状态（从配置推导）
            s_rules_status = {}
            try:
                s_rules_config = db.get_config("s_rules", {})
                
                # S0: 仓位计算方法
                s0_method = s_rules_config.get("s0_position_sizing", "fixed")
                s_rules_status["s0"] = {
                    "passed": True,
                    "message": {"fixed": "固定金额", "percentage": "百分比", "shares": "股数"}.get(s0_method, s0_method)
                }
                
                # S1: 仓位数值/百分比
                s1_value = s_rules_config.get("s1_risk_percentage", 1.0)
                s_rules_status["s1"] = {
                    "passed": True,
                    "message": f"{s1_value}{'%' if s0_method == 'percentage' else (' 股' if s0_method == 'shares' else '$')}"
                }
                
                # S2: 最大仓位数量
                s2_max = s_rules_config.get("s2_max_positions", 10)
                s_rules_status["s2"] = {
                    "passed": True,
                    "message": str(s2_max)
                }
                
                # S3: 仓位重平衡
                s3_enabled = s_rules_config.get("s3_volatility_adjustment", True)
                s_rules_status["s3"] = {
                    "passed": True,
                    "message": "启用" if s3_enabled else "禁用"
                }
                
                # S4: 波动率调整
                s4_enabled = s_rules_config.get("s4_volatility_adjustment", True)
                s_rules_status["s4"] = {
                    "passed": True,
                    "message": "启用" if s4_enabled else "禁用"
                }
                
            except Exception as config_error:
                logger.warning(f"Failed to load S rules config: {config_error}")
                # 提供默认S规则状态
                for i in range(5):  # S0-S4
                    s_rules_status[f"s{i}"] = {"passed": True, "message": "正常"}
            
            # 添加到历史记录中
            item['g_rules_status'] = g_rules_status
            item['s_rules_status'] = s_rules_status
        
        return {"success": True, "data": items, "count": len(items)}
    except Exception as e:
        logger.error(f"Get sizing history error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# 配置管理接口 - 统一管理G Rules和S Rules
@router.get("/api/config/rules")
async def get_config_rules(user: Optional[str] = Depends(get_current_user)):
    try:
        db = get_db_manager()
        
        # 从新的config表获取配置
        g_rules = db.get_config("g_rules", {})
        s_rules = db.get_config("s_rules", {})
        
        # 如果config表中没有数据，尝试从旧的strategy_settings迁移
        if not g_rules and not s_rules:
            settings = db.load_strategy_settings()
            if isinstance(settings, dict):
                g_rules = settings.get("g_rules_config", {})
                s_rules = settings.get("sizing_policy", {})
                
                # 迁移到新的config表
                if g_rules:
                    db.save_config("g_rules", g_rules, "G Rules配置")
                if s_rules:
                    db.save_config("s_rules", s_rules, "S Rules配置")
        
        return {"g_rules": g_rules, "s_rules": s_rules}
    except Exception as e:
        logger.error(f"Get config rules error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/api/config/rules")
async def update_config_rules(payload: Dict[str, Any], user: Optional[str] = Depends(get_current_user)):
    try:
        db = get_db_manager()
        
        # 保存G Rules和S Rules到config表
        if "g_rules" in payload:
            db.save_config("g_rules", payload["g_rules"], "G Rules配置")
        
        if "s_rules" in payload:
            db.save_config("s_rules", payload["s_rules"], "S Rules配置")
        
        # 读回确认
        g_rules = db.get_config("g_rules", {})
        s_rules = db.get_config("s_rules", {})
        
        return {"success": True, "g_rules": g_rules, "s_rules": s_rules}
    except Exception as e:
        logger.error(f"Update config rules error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# WebSocket endpoint
