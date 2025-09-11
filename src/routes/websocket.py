import asyncio
import json
from typing import Dict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

from core import connection_manager, ibkr_client, websocket_manager, order_manager

router = APIRouter()

async def _realtime_price_sender(ws: WebSocket, symbol: str, interval: float = 2.0):
    try:
        loop = asyncio.get_event_loop()
        while True:
            if not connection_manager.is_connected() or 'ibkr_client' not in globals() or ibkr_client is None:
                await asyncio.sleep(interval)
                continue
            try:
                data = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: ibkr_client.get_real_time_data(symbol, "SMART")),
                    timeout=10.0
                )
                await ws.send_text(json.dumps({
                    "type": "realtime_data",
                    "symbol": symbol,
                    "data": data
                }))
            except Exception as e:
                logger.debug(f"Realtime data error for {symbol}: {e}")
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        pass

@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket_manager.connect(websocket)
    # 初次连接后，向该连接单播发送订单快照（最多300条）
    try:
        if 'order_manager' in globals() and order_manager:
            loop = asyncio.get_event_loop()
            orders = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: order_manager.get_orders(limit=300)),
                timeout=5.0
            )
            converted_orders = []
            for o in orders:
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
                    'submit_time': (o.created_at.isoformat() if getattr(o, 'created_at', None) else None),
                    'order_source': getattr(o, 'order_source', 'manual'),
                    'trigger_type': trigger_type,
                    'commission': getattr(o, 'commission', None),
                    'pnl': getattr(o, 'pnl', None)
                }
                converted_orders.append(converted_order)
            await websocket.send_text(json.dumps({'type': 'orders_snapshot', 'orders': converted_orders}))
    except Exception as e:
        logger.warning(f"Failed to send initial orders snapshot: {e}")
    realtime_tasks: Dict[str, asyncio.Task] = {}
    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            msg_type = message.get("type")
            if msg_type == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
            elif msg_type == "subscribe_realtime":
                symbol = message.get("symbol")
                if symbol and symbol not in realtime_tasks:
                    realtime_tasks[symbol] = asyncio.create_task(_realtime_price_sender(websocket, symbol))
            elif msg_type == "unsubscribe_realtime":
                symbol = message.get("symbol")
                task = realtime_tasks.pop(symbol, None)
                if task:
                    task.cancel()
    except WebSocketDisconnect:
        for task in realtime_tasks.values():
            task.cancel()
        websocket_manager.disconnect(websocket)

