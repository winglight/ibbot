import asyncio
import json
import time
from datetime import timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

import core
from core import (
    connection_manager,
    websocket_manager,
    order_manager,
    strategy_manager,
    risk_manager,
)
from src.database import get_db_manager
from src.dom_optimizer import get_dom_optimization_orchestrator
from src.dom_service import get_dom_service
from src.utils import now_local, parse_local_datetime, to_local_iso

router = APIRouter()

RATE_LIMIT_SECONDS = 15.0


def _serialise_order(order: Any) -> Dict[str, Any]:
    raw_type = (getattr(order, "order_type", "") or "").upper()
    if raw_type in ("MARKET", "MKT"):
        order_type = "MKT"
    elif raw_type in ("LIMIT", "LMT"):
        order_type = "LMT"
    else:
        order_type = raw_type or "MKT"

    price_val = 0.0
    try:
        price = getattr(order, "price", None)
        if price is not None:
            price_val = float(price)
    except Exception:
        price_val = 0.0

    trigger_type = None
    try:
        if getattr(order, "order_source", "manual") == "auto":
            rule_id = getattr(order, "rule_id", None)
            symbol = getattr(order, "symbol", None)
            if rule_id and rule_id != symbol:
                trigger_type = "strategy"
            else:
                trigger_type = "risk_rule"
    except Exception:
        trigger_type = None

    created_at = getattr(order, "created_at", None)
    created_iso = None
    try:
        created_iso = to_local_iso(created_at)
    except Exception:
        if created_at is not None:
            try:
                created_iso = created_at.isoformat()
            except Exception:
                created_iso = None

    avg_fill_price = getattr(order, "fill_price", None)
    if avg_fill_price is None and order_type == "MKT" and price_val > 0:
        avg_fill_price = price_val

    return {
        "order_id": getattr(order, "order_id", None),
        "symbol": getattr(order, "symbol", None),
        "action": getattr(order, "action", None),
        "quantity": getattr(order, "quantity", None),
        "order_type": order_type,
        "limit_price": price_val if order_type == "LMT" and price_val > 0 else None,
        "avg_fill_price": avg_fill_price,
        "status": getattr(order, "status", None),
        "filled": getattr(order, "quantity", 0) if getattr(order, "status", None) == "Filled" else 0,
        "remaining": 0 if getattr(order, "status", None) == "Filled" else getattr(order, "quantity", 0),
        "submit_time": created_iso,
        "order_source": getattr(order, "order_source", "manual"),
        "strategy_name": getattr(order, "strategy_name", None),
        "trigger_type": trigger_type,
        "commission": getattr(order, "commission", None),
        "pnl": getattr(order, "pnl", None),
    }


def _orders_payload(
    orders: List[Any], *, limit: Optional[int] = None, start_date: Optional[str] = None
) -> Dict[str, Any]:
    return {
        "type": "orders_snapshot",
        "success": True,
        "orders": [_serialise_order(order) for order in orders],
        "meta": {
            "limit": limit,
            "start_date": start_date,
            "generated_at": to_local_iso(now_local()),
            "source": "websocket",
        },
    }


def _allow_request(tracker: Dict[str, float], key: str, *, force: bool = False) -> bool:
    now = time.monotonic()
    if force:
        tracker[key] = now
        return True
    last = tracker.get(key, 0.0)
    if now - last >= RATE_LIMIT_SECONDS:
        tracker[key] = now
        return True
    return False


def _normalise_rule_status(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        passed = bool(value.get("passed", True))
        message = value.get("message")
        if not message:
            errors = value.get("errors")
            if isinstance(errors, list):
                message = " | ".join(str(err) for err in errors if err)
            elif errors:
                message = str(errors)
        if not message:
            message = "正常" if passed else "违规"
        return {"passed": passed, "message": message}
    return {"passed": True, "message": "正常"}


def _enrich_sizing_history_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not items:
        return items

    db = get_db_manager()
    try:
        s_rules_config = db.get_config("s_rules", {}) or {}
    except Exception as exc:
        logger.warning(f"Failed to load S rules config for sizing history: {exc}")
        s_rules_config = {}

    for item in items:
        g_rules_checked = item.get("g_rules_checked", [])
        if isinstance(g_rules_checked, str):
            try:
                g_rules_checked = json.loads(g_rules_checked)
            except (json.JSONDecodeError, TypeError):
                g_rules_checked = []

        g_rules_status: Dict[str, Dict[str, Any]] = {}
        if isinstance(g_rules_checked, list):
            for idx, rule_result in enumerate(g_rules_checked):
                if isinstance(rule_result, dict):
                    g_rules_status[f"g{idx}"] = _normalise_rule_status(rule_result)

        if not g_rules_status:
            for idx in range(6):
                g_rules_status[f"g{idx}"] = {"passed": True, "message": "正常"}

        s_rules_status: Dict[str, Dict[str, Any]] = {}
        try:
            s0_method = s_rules_config.get("s0_position_sizing", "fixed")
            method_map = {"fixed": "固定金额", "percentage": "百分比", "shares": "股数"}
            s_rules_status["s0"] = {
                "passed": True,
                "message": method_map.get(s0_method, s0_method),
            }

            s1_value = s_rules_config.get("s1_risk_percentage", 1.0)
            suffix = "%" if s0_method == "percentage" else (" 股" if s0_method == "shares" else "$")
            s_rules_status["s1"] = {
                "passed": True,
                "message": f"{s1_value}{suffix}",
            }

            s2_max = s_rules_config.get("s2_max_positions", 10)
            s_rules_status["s2"] = {"passed": True, "message": str(s2_max)}

            s3_enabled = s_rules_config.get("s3_volatility_adjustment", True)
            s_rules_status["s3"] = {
                "passed": True,
                "message": "启用" if s3_enabled else "禁用",
            }

            s4_enabled = s_rules_config.get("s4_volatility_adjustment", True)
            s_rules_status["s4"] = {
                "passed": True,
                "message": "启用" if s4_enabled else "禁用",
            }
        except Exception as exc:
            logger.warning(f"Failed to derive S rules status: {exc}")
            for idx in range(5):
                s_rules_status[f"s{idx}"] = {"passed": True, "message": "正常"}

        item["g_rules_status"] = g_rules_status
        item["s_rules_status"] = s_rules_status

    return items

async def _realtime_price_sender(ws: WebSocket, symbol: str, interval: float = 2.0):
    try:
        loop = asyncio.get_event_loop()
        while True:
            if not connection_manager.is_connected():
                await asyncio.sleep(interval)
                continue
            try:
                data = await core.ibkr_client.get_real_time_data(symbol, "SMART")
                # 只有当数据有效时才发送
                if data is not None:
                    await ws.send_text(json.dumps({
                        "type": "realtime_data",
                        "symbol": symbol,
                        "data": data
                    }))
                else:
                    logger.debug(f"Skipping null realtime data for {symbol}")
            except Exception as e:
                logger.debug(f"Realtime data error for {symbol}: {e}")
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        pass

@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket_manager.connect(websocket)

    # 新连接建立后，立即向该客户端单播当前连接状态
    try:
        await websocket.send_text(json.dumps({
            "type": "connection_status",
            "connected": connection_manager.is_connected(),
            "account_type": "Paper" if connection_manager.config.paper_trading else "Live",
        }))
    except Exception as e:
        logger.debug(f"Failed to send initial connection status: {e}")

    # 初次连接后，向该连接单播账户信息快照
    try:
        if connection_manager.is_connected():
            try:
                info = await asyncio.wait_for(core.ibkr_client.get_account_info(), timeout=8.0)
                await websocket.send_text(json.dumps({
                    "type": "account_update",
                    "account_info": info
                }))
            except asyncio.TimeoutError:
                logger.warning("Initial account_info snapshot timed out on websocket connect")
            except Exception as e:
                logger.warning(f"Failed to get/send initial account_info snapshot: {e}")
        else:
            # 未连接时也发送一个占位 account_update，便于前端立即渲染“未连接”状态
            last_err = None
            try:
                last_err = connection_manager.get_last_connect_error()
            except Exception:
                last_err = None
            await websocket.send_text(json.dumps({
                "type": "account_update",
                "account_info": {"connected": False, "last_error": last_err}
            }))
    except Exception as e:
        logger.debug(f"Failed to send initial account_update: {e}")

    # 初次连接后，向该连接单播发送订单快照（最多300条）
    try:
        if 'order_manager' in globals() and order_manager:
            loop = asyncio.get_event_loop()
            orders = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: order_manager.get_orders(limit=300)),
                timeout=5.0
            )
            payload = _orders_payload(orders, limit=300)
            await websocket.send_text(json.dumps(payload))
    except Exception as e:
        logger.warning(f"Failed to send initial orders snapshot: {e}")
    
    # 初次连接后，向该连接单播发送positions快照
    try:
        if connection_manager.is_connected():
            try:
                positions = await asyncio.wait_for(core.ibkr_client.get_positions(fast=True), timeout=8.0)
                await websocket.send_text(json.dumps({
                    "type": "position_update",
                    "positions": positions
                }))
            except asyncio.TimeoutError:
                logger.warning("Initial positions snapshot timed out on websocket connect")
            except Exception as e:
                logger.warning(f"Failed to get/send initial positions snapshot: {e}")
    except Exception as e:
        logger.debug(f"Failed to send initial positions snapshot: {e}")
    
    realtime_tasks: Dict[str, asyncio.Task] = {}
    request_tracker: Dict[str, float] = {}
    last_responses: Dict[str, Dict[str, Any]] = {}
    dom_service = get_dom_service()
    orchestrator = None
    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            msg_type = message.get("type")
            if msg_type == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
            elif msg_type == "request_connection_status":
                key = "connection_status"
                force = bool(message.get("force"))
                if not _allow_request(request_tracker, key, force=force):
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        cached_payload["cached"] = True
                        await websocket.send_text(json.dumps(cached_payload))
                    else:
                        await websocket.send_text(
                            json.dumps(
                                {
                                    "type": "connection_status",
                                    "success": False,
                                    "error": "Rate limited",
                                }
                            )
                        )
                    continue

                try:
                    is_connected = await asyncio.wait_for(
                        asyncio.to_thread(connection_manager.is_connected),
                        timeout=1.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning("Connection status request timed out; using cached state")
                    is_connected = getattr(connection_manager, "connected", False)
                except Exception as exc:
                    logger.warning(f"Connection status request failed: {exc}")
                    is_connected = getattr(connection_manager, "connected", False)

                account_type = "Paper"
                try:
                    if not connection_manager.config.paper_trading:
                        account_type = "Live"
                except Exception:
                    account_type = "Paper"

                payload = {
                    "type": "connection_status",
                    "connected": bool(is_connected),
                    "account_type": account_type,
                    "success": True,
                    "source": "websocket",
                }

                try:
                    last_error = connection_manager.get_last_connect_error()
                    if last_error:
                        payload["last_error"] = last_error
                except Exception:
                    pass

                last_responses[key] = payload
                await websocket.send_text(json.dumps(payload))
            elif msg_type == "subscribe_realtime":
                symbol = message.get("symbol")
                if symbol and symbol not in realtime_tasks:
                    realtime_tasks[symbol] = asyncio.create_task(_realtime_price_sender(websocket, symbol))
            elif msg_type == "unsubscribe_realtime":
                symbol = message.get("symbol")
                task = realtime_tasks.pop(symbol, None)
                if task:
                    task.cancel()
            elif msg_type == "request_orders_snapshot":
                if 'order_manager' not in globals() or not order_manager:
                    await websocket.send_text(json.dumps({
                        "type": "orders_snapshot",
                        "success": False,
                        "error": "Order manager unavailable",
                    }))
                    continue

                try:
                    limit = int(message.get("limit", 50))
                except (TypeError, ValueError):
                    limit = 50
                start_date_raw = message.get("start_date")
                start_dt = parse_local_datetime(start_date_raw)
                key = f"orders_snapshot:{limit}:{start_date_raw}"
                force = bool(message.get("force"))
                if not _allow_request(request_tracker, key, force=force):
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        cached_payload["cached"] = True
                        await websocket.send_text(json.dumps(cached_payload))
                    else:
                        await websocket.send_text(json.dumps({
                            "type": "orders_snapshot",
                            "success": False,
                            "error": "Rate limited",
                        }))
                    continue

                loop = asyncio.get_event_loop()

                try:
                    orders = await asyncio.wait_for(
                        loop.run_in_executor(
                            None,
                            lambda: order_manager.get_orders(
                                limit=limit,
                                start_date=start_dt,
                            ),
                        ),
                        timeout=6.0,
                    )
                    payload = _orders_payload(
                        orders,
                        limit=limit,
                        start_date=start_date_raw,
                    )
                    last_responses[key] = payload
                    await websocket.send_text(json.dumps(payload))
                except Exception as exc:
                    logger.warning(f"Failed to stream orders snapshot: {exc}")
                    await websocket.send_text(json.dumps({
                        "type": "orders_snapshot",
                        "success": False,
                        "error": str(exc),
                    }))
            elif msg_type == "request_positions_snapshot":
                key = "positions_snapshot"
                force = bool(message.get("force"))

                if not connection_manager or not connection_manager.is_connected():
                    payload = {
                        "type": "position_update",
                        "success": False,
                        "error": "Not connected to IBKR",
                        "source": "websocket",
                        "positions": [],
                    }
                    await websocket.send_text(json.dumps(payload))
                    continue

                use_cache = message.get("use_cache")
                use_cache = bool(use_cache) if use_cache is not None else False

                cached = last_responses.get(key)
                if use_cache and cached and not force:
                    cached_payload = dict(cached)
                    cached_payload["cached"] = True
                    await websocket.send_text(json.dumps(cached_payload))
                    continue

                if not _allow_request(request_tracker, key, force=force):
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        cached_payload["cached"] = True
                        await websocket.send_text(json.dumps(cached_payload))
                    else:
                        await websocket.send_text(json.dumps({
                            "type": "position_update",
                            "success": False,
                            "error": "Rate limited",
                            "source": "websocket",
                        }))
                    continue

                fast_flag = message.get("fast")
                fast = bool(fast_flag) if fast_flag is not None else False

                ib_client = getattr(core, "ibkr_client", None)
                if ib_client is None:
                    await websocket.send_text(json.dumps({
                        "type": "position_update",
                        "success": False,
                        "error": "IBKR client unavailable",
                        "source": "websocket",
                    }))
                    continue

                try:
                    positions = await asyncio.wait_for(
                        ib_client.get_positions(fast=fast),
                        timeout=6.0,
                    )
                    payload = {
                        "type": "position_update",
                        "success": True,
                        "positions": positions,
                        "source": "websocket",
                        "fast": fast,
                    }
                    last_responses[key] = payload
                    await websocket.send_text(json.dumps(payload))
                except asyncio.TimeoutError:
                    logger.warning("Positions snapshot request timed out")
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        cached_payload["cached"] = True
                        cached_payload["timeout"] = True
                        await websocket.send_text(json.dumps(cached_payload))
                    else:
                        await websocket.send_text(json.dumps({
                            "type": "position_update",
                            "success": False,
                            "error": "Positions request timed out",
                            "source": "websocket",
                        }))
                except Exception as exc:
                    logger.warning(f"Failed to stream positions snapshot: {exc}")
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        cached_payload["cached"] = True
                        cached_payload["error"] = str(exc)
                        await websocket.send_text(json.dumps(cached_payload))
                    else:
                        await websocket.send_text(json.dumps({
                            "type": "position_update",
                            "success": False,
                            "error": str(exc),
                            "source": "websocket",
                        }))
            elif msg_type == "request_dom_automation_status":
                if orchestrator is None:
                    try:
                        orchestrator = get_dom_optimization_orchestrator()
                    except Exception as exc:
                        logger.warning(f"Failed to init DOM orchestrator: {exc}")
                        orchestrator = None
                if orchestrator is None:
                    await websocket.send_text(json.dumps({
                        "type": "dom_automation_status",
                        "success": False,
                        "error": "DOM orchestrator unavailable",
                    }))
                    continue

                force = bool(message.get("force"))
                key = "dom_automation_status"
                if not _allow_request(request_tracker, key, force=force):
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        cached_payload["cached"] = True
                        await websocket.send_text(json.dumps(cached_payload))
                    else:
                        await websocket.send_text(json.dumps({
                            "type": "dom_automation_status",
                            "success": False,
                            "error": "Rate limited",
                        }))
                    continue

                try:
                    status = await asyncio.to_thread(orchestrator.get_status)
                    payload = {
                        "type": "dom_automation_status",
                        "success": True,
                        "status": status,
                    }
                    if isinstance(status, dict):
                        payload.update(status)
                    last_responses[key] = payload
                    await websocket.send_text(json.dumps(payload))
                except Exception as exc:
                    logger.warning(f"Failed to fetch DOM automation status: {exc}")
                    await websocket.send_text(json.dumps({
                        "type": "dom_automation_status",
                        "success": False,
                        "error": str(exc),
                    }))
            elif msg_type == "request_dom_stats":
                symbol = (message.get("symbol") or "").strip().upper()
                if not symbol:
                    await websocket.send_text(json.dumps({
                        "type": "dom_stats",
                        "success": False,
                        "error": "Symbol is required",
                    }))
                    continue

                try:
                    lookback_seconds = message.get("lookback_seconds")
                    lookback_val = int(lookback_seconds) if lookback_seconds is not None else None
                except (TypeError, ValueError):
                    lookback_val = None

                try:
                    max_points_raw = message.get("max_points")
                    max_points = int(max_points_raw) if max_points_raw is not None else None
                except (TypeError, ValueError):
                    max_points = None

                start_param = message.get("start")
                end_param = message.get("end")
                key = f"dom_stats:{symbol}:{lookback_val}:{max_points}:{start_param}:{end_param}"
                force = bool(message.get("force"))
                if not _allow_request(request_tracker, key, force=force):
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        cached_payload["cached"] = True
                        await websocket.send_text(json.dumps(cached_payload))
                    else:
                        await websocket.send_text(json.dumps({
                            "type": "dom_stats",
                            "success": False,
                            "symbol": symbol,
                            "error": "Rate limited",
                        }))
                    continue

                try:
                    result = await dom_service.get_stats_async(
                        symbol,
                        lookback_seconds=lookback_val,
                        start_time=start_param,
                        end_time=end_param,
                        max_points=max_points,
                    )
                    payload = {"type": "dom_stats", "success": True, "symbol": symbol}
                    if isinstance(result, dict):
                        payload.update(result)
                    last_responses[key] = payload
                    await websocket.send_text(json.dumps(payload))
                except Exception as exc:
                    logger.warning(f"Failed to fetch DOM stats: {exc}")
                    await websocket.send_text(json.dumps({
                        "type": "dom_stats",
                        "success": False,
                        "symbol": symbol,
                        "error": str(exc),
                    }))
            elif msg_type == "request_risk_rules":
                if 'risk_manager' not in globals() or risk_manager is None:
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "risk_rules",
                                "success": False,
                                "error": "Risk manager unavailable",
                            }
                        )
                    )
                    continue

                key = "risk_rules"
                force = bool(message.get("force"))
                if not _allow_request(request_tracker, key, force=force):
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        cached_payload["cached"] = True
                        await websocket.send_text(json.dumps(cached_payload))
                    else:
                        await websocket.send_text(
                            json.dumps(
                                {
                                    "type": "risk_rules",
                                    "success": False,
                                    "error": "Rate limited",
                                }
                            )
                        )
                    continue

                try:
                    loop = asyncio.get_event_loop()
                    rules = await asyncio.wait_for(
                        loop.run_in_executor(None, risk_manager.get_risk_rules),
                        timeout=5.0,
                    )
                    serialisable = {}
                    for symbol, rule in (rules or {}).items():
                        serialisable[symbol] = {
                            "symbol": getattr(rule, "symbol", symbol),
                            "stop_loss_price": getattr(rule, "stop_loss_price", None),
                            "take_profit_price": getattr(rule, "take_profit_price", None),
                            "max_loss_percent": getattr(rule, "max_loss_percent", None),
                            "max_time_span": getattr(rule, "max_time_span", None),
                            "enabled": getattr(rule, "enabled", False),
                            "created_at": getattr(rule, "created_at", None).isoformat()
                            if getattr(rule, "created_at", None)
                            else None,
                        }
                    payload = {
                        "type": "risk_rules",
                        "success": True,
                        "rules": serialisable,
                        "source": "websocket",
                    }
                    last_responses[key] = payload
                    await websocket.send_text(json.dumps(payload))
                except Exception as exc:
                    logger.warning(f"Failed to stream risk rules: {exc}")
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "risk_rules",
                                "success": False,
                                "error": str(exc),
                            }
                        )
                    )
            elif msg_type == "request_sizing_history":
                try:
                    limit = int(message.get("limit", 50))
                except (TypeError, ValueError):
                    limit = 50
                limit = max(1, min(limit, 500))

                symbol = (message.get("symbol") or "").strip() or None
                since_raw = message.get("since")
                context = message.get("context")

                key = f"sizing_history:{symbol or ''}:{since_raw or ''}:{limit}"
                force = bool(message.get("force"))
                if not _allow_request(request_tracker, key, force=force):
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        if context is not None:
                            cached_payload = dict(cached_payload)
                            cached_payload["context"] = context
                        cached_payload["cached"] = True
                        await websocket.send_text(json.dumps(cached_payload))
                    else:
                        await websocket.send_text(
                            json.dumps(
                                {
                                    "type": "sizing_history",
                                    "success": False,
                                    "error": "Rate limited",
                                    "context": context,
                                }
                            )
                        )
                    continue

                try:
                    db = get_db_manager()
                    items = db.load_sizing_history(symbol=symbol, since=since_raw, limit=limit)
                    enriched = _enrich_sizing_history_items(items)
                    meta = {
                        "limit": limit,
                        "symbol": symbol,
                        "since": since_raw,
                        "generated_at": to_local_iso(now_local()),
                        "source": "websocket",
                    }
                    base_payload = {
                        "type": "sizing_history",
                        "success": True,
                        "data": enriched,
                        "meta": meta,
                    }
                    last_responses[key] = base_payload
                    payload = dict(base_payload)
                    if context is not None:
                        payload["context"] = context
                    await websocket.send_text(json.dumps(payload))
                except Exception as exc:
                    logger.warning(f"Failed to stream sizing history: {exc}")
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "sizing_history",
                                "success": False,
                                "error": str(exc),
                                "context": context,
                            }
                        )
                    )
            elif msg_type == "request_strategies":
                if 'strategy_manager' not in globals() or strategy_manager is None:
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "strategies_snapshot",
                                "success": False,
                                "error": "Strategy manager unavailable",
                            }
                        )
                    )
                    continue

                key = "strategies_snapshot"
                force = bool(message.get("force"))
                if not _allow_request(request_tracker, key, force=force):
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        cached_payload["cached"] = True
                        await websocket.send_text(json.dumps(cached_payload))
                    else:
                        await websocket.send_text(
                            json.dumps(
                                {
                                    "type": "strategies_snapshot",
                                    "success": False,
                                    "error": "Rate limited",
                                }
                            )
                        )
                    continue

                try:
                    loop = asyncio.get_event_loop()
                    strategies = await asyncio.wait_for(
                        loop.run_in_executor(None, strategy_manager.get_strategies),
                        timeout=6.0,
                    )
                    payload = {
                        "type": "strategies_snapshot",
                        "success": True,
                        "strategies": strategies or [],
                        "source": "websocket",
                    }
                    last_responses[key] = payload
                    await websocket.send_text(json.dumps(payload))
                except Exception as exc:
                    logger.warning(f"Failed to stream strategies: {exc}")
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "strategies_snapshot",
                                "success": False,
                                "error": str(exc),
                            }
                        )
                    )
            elif msg_type == "request_strategy_status":
                if 'strategy_manager' not in globals() or strategy_manager is None:
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "strategy_status",
                                "success": False,
                                "error": "Strategy manager unavailable",
                            }
                        )
                    )
                    continue

                name = (message.get("name") or "").strip()
                if not name:
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "strategy_status",
                                "success": False,
                                "error": "Strategy name is required",
                            }
                        )
                    )
                    continue

                key = f"strategy_status:{name}"
                force = bool(message.get("force"))
                if not _allow_request(request_tracker, key, force=force):
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        cached_payload["cached"] = True
                        await websocket.send_text(json.dumps(cached_payload))
                    else:
                        await websocket.send_text(
                            json.dumps(
                                {
                                    "type": "strategy_status",
                                    "success": False,
                                    "name": name,
                                    "error": "Rate limited",
                                }
                            )
                        )
                    continue

                try:
                    loop = asyncio.get_event_loop()
                    status = await asyncio.wait_for(
                        loop.run_in_executor(None, strategy_manager.get_internal_strategy_status, name),
                        timeout=5.0,
                    )
                    if status is None:
                        raise ValueError("Strategy status unavailable")
                    payload = {
                        "type": "strategy_status",
                        "success": True,
                        "name": name,
                        "status": status,
                        "source": "websocket",
                    }
                    last_responses[key] = payload
                    await websocket.send_text(json.dumps(payload))
                except Exception as exc:
                    logger.warning(f"Failed to fetch strategy status for {name}: {exc}")
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "strategy_status",
                                "success": False,
                                "name": name,
                                "error": str(exc),
                            }
                        )
                    )
            elif msg_type == "request_strategy_performance":
                name = (message.get("name") or "").strip()
                if not name:
                    await websocket.send_text(json.dumps({
                        "type": "strategy_performance",
                        "success": False,
                        "error": "Strategy name is required",
                    }))
                    continue

                range_value = (message.get("range") or "1d").strip()
                start_raw = message.get("start")
                end_raw = message.get("end")
                start_date = parse_local_datetime(start_raw)
                end_date = parse_local_datetime(end_raw)

                if range_value:
                    try:
                        now = now_local()
                        if range_value == "1d":
                            start_date = now - timedelta(days=1)
                        elif range_value == "1w":
                            start_date = now - timedelta(weeks=1)
                        elif range_value == "1m":
                            start_date = now - timedelta(days=30)
                        elif range_value == "wtd":
                            start_date = now - timedelta(days=now.weekday())
                        elif range_value == "mtd":
                            start_date = now.replace(day=1)
                        elif range_value == "ytd":
                            start_date = now.replace(month=1, day=1)
                    except Exception:
                        pass

                key = f"strategy_performance:{name}:{range_value}:{start_raw}:{end_raw}"
                force = bool(message.get("force"))
                if not _allow_request(request_tracker, key, force=force):
                    cached = last_responses.get(key)
                    if cached:
                        cached_payload = dict(cached)
                        cached_payload["cached"] = True
                        await websocket.send_text(json.dumps(cached_payload))
                    else:
                        await websocket.send_text(json.dumps({
                            "type": "strategy_performance",
                            "success": False,
                            "name": name,
                            "range": range_value,
                            "error": "Rate limited",
                        }))
                    continue

                loop = asyncio.get_event_loop()
                try:
                    metrics = await asyncio.wait_for(
                        loop.run_in_executor(
                            None,
                            lambda: strategy_manager.get_strategy_performance(
                                name,
                                start_date=start_date,
                                end_date=end_date,
                            ),
                        ),
                        timeout=6.0,
                    )
                    payload = {
                        "type": "strategy_performance",
                        "success": True,
                        "name": name,
                        "range": range_value,
                        "metrics": metrics,
                    }
                    last_responses[key] = payload
                    await websocket.send_text(json.dumps(payload))
                except Exception as exc:
                    logger.warning(f"Failed to fetch strategy performance: {exc}")
                    await websocket.send_text(json.dumps({
                        "type": "strategy_performance",
                        "success": False,
                        "name": name,
                        "error": str(exc),
                    }))
    except WebSocketDisconnect:
        for task in realtime_tasks.values():
            task.cancel()
        websocket_manager.disconnect(websocket)

