#!/usr/bin/env python3
"""
IBKR Trading Bot - Main Application

A comprehensive trading bot for Interactive Brokers with:
- Real-time market data
- Automated trading
- Risk management
- Web UI
- Telegram notifications
"""

import asyncio
import os
import sys
import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from loguru import logger
from dotenv import load_dotenv
from dataclasses import asdict

# FastAPI imports
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Depends, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from jose import JWTError, jwt

# Add current directory to Python path
sys.path.append(str(Path(__file__).parent))

from src.ibkr_client import IBKRClient
from src.telegram_notifier import TelegramNotifier
from src.risk_manager import RiskManager
from src.connection_manager import connection_manager, ConnectionConfig
from src.strategy_manager import StrategyManager
from src.database import get_db_manager
from src.strategy_manager import StrategyExecution
from src.queue_manager import queue_manager
from src.order_manager import OrderManager
from src.models import HistoricalDataRequest, ChartDataRequest, OrderRequest, ExternalStrategyOrderRequest, StrategySettingsRequest, StrategyRequest, RiskRuleRequest
from src.websocket_manager import websocket_manager  # 新增导入

# Global variables
load_dotenv()

# FastAPI app
app = FastAPI(title="IBKR Trading Bot", version="1.0.0")

# 挂载静态资源目录
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

# 根路由返回前端页面
@app.get("/")
async def index_page():
    index_path = Path(__file__).parent / "static" / "index.html"
    if not index_path.exists():
        # 若前端文件缺失，返回简单提示
        return HTMLResponse("<h3>Front-end index.html not found under /static</h3>", status_code=500)
    return FileResponse(str(index_path))

# Auth config
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'change_me')
JWT_SECRET = os.getenv('JWT_SECRET', 'please_change_me')
JWT_ALGORITHM = os.getenv('JWT_ALGORITHM', 'HS256')
JWT_EXPIRE_DAYS = int(os.getenv('JWT_EXPIRE_DAYS', '1'))

# Utility helpers

def print_banner():
    banner = (
        "\n"
        "==============================================\n"
        "   IBKR Trading Bot - FastAPI Service\n"
        "==============================================\n"
    )
    # 使用 print，因为此时日志可能尚未初始化
    print(banner)


def setup_logging():
    """Configure loguru to log to console and file.
    确保 logs/trading_bot.log 存在，并设置合理的滚动与保留策略。
    """
    try:
        os.makedirs("logs", exist_ok=True)
    except Exception:
        # 即便创建失败也不要崩溃
        pass

    # 重置默认 handler，添加控制台和文件输出
    try:
        logger.remove()
    except Exception:
        pass

    # 统一格式，便于 /api/logs 按等级过滤：会匹配 "| LEVEL |"
    log_format = "| {time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {name}:{function}:{line} - {message}"

    # 控制台
    logger.add(sys.stderr, level="INFO", format=log_format, enqueue=True, backtrace=False, diagnose=False)

    # 文件
    logger.add(
        "logs/trading_bot.log",
        rotation="10 MB",
        retention="14 days",
        encoding="utf-8",
        enqueue=True,
        level="INFO",
        format=log_format,
        backtrace=False,
        diagnose=False,
    )


def create_directories():
    """Create required directories used by the app."""
    for d in ["logs", "data", "strategy"]:
        try:
            os.makedirs(d, exist_ok=True)
        except Exception as e:
            # 不阻断启动，仅记录警告
            try:
                logger.warning(f"Create directory failed for {d}: {e}")
            except Exception:
                # 如果 logger 未初始化，静默忽略
                pass


def check_environment() -> bool:
    """Basic environment checks. Returns True if ok.
    仅做基础校验：.env 可选；若关键变量使用默认值，给出警告但不阻断启动。
    """
    ok = True
    defaults_used = []

    if os.getenv('JWT_SECRET', 'please_change_me') == 'please_change_me':
        defaults_used.append('JWT_SECRET')
    if os.getenv('ADMIN_PASSWORD', 'change_me') == 'change_me':
        defaults_used.append('ADMIN_PASSWORD')
    if os.getenv('ADMIN_USERNAME', 'admin') == 'admin':
        defaults_used.append('ADMIN_USERNAME')

    if defaults_used:
        logger.warning(f"Using default values for: {', '.join(defaults_used)}. Please set them in your .env file.")

    # 端口与主机合法性（基础校验）
    try:
        int(os.getenv('WEB_PORT', '8000'))
    except ValueError:
        logger.error("Invalid WEB_PORT. It must be an integer.")
        ok = False

    return ok
class LoginRequest(BaseModel):
    username: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = 'bearer'
    expires_in: int

EXEMPT_PATHS = set([
    '/',
    '/static/style.css',
    '/static/app.js',
    '/static',
    '/api/login',
    '/api/strategy',  # external strategies use body token
])

def create_access_token(data: dict, expires_delta: timedelta | None = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(days=JWT_EXPIRE_DAYS))
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return encoded_jwt, int(expires_delta.total_seconds() if expires_delta else timedelta(days=JWT_EXPIRE_DAYS).total_seconds())

async def get_current_user(request: Request):
    # Strategy endpoint uses body token; allow it via EXEMPT_PATHS
    if request.url.path in EXEMPT_PATHS:
        return None
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        raise HTTPException(status_code=401, detail='Unauthorized')
    token = auth_header.split(' ', 1)[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload.get('sub')
    except JWTError:
        raise HTTPException(status_code=401, detail='Unauthorized')

@app.post('/api/login')
async def login(req: LoginRequest):
    if req.username != ADMIN_USERNAME or req.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail='Invalid credentials')
    token, expires_in = create_access_token({"sub": req.username})
    return {"access_token": token, "token_type": "bearer", "expires_in": expires_in}


# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global instances
ibkr_client = None
telegram_notifier = TelegramNotifier()  # 初始化
risk_manager = RiskManager(ibkr_client=None, telegram_notifier=telegram_notifier)  # 先用None占位，连接成功后依赖连接管理器
strategy_manager = StrategyManager()  # 初始化
websocket_connections = []
order_manager = OrderManager()

# 在应用启动时注入依赖（即使部分为None，也可在连接后更新）
try:
    queue_manager.set_dependencies(
        ibkr_client=None,
        risk_manager=risk_manager,
        strategy_manager=strategy_manager,
        connection_manager=connection_manager,
        websocket_manager=websocket_manager,
        order_manager=order_manager
    )
except Exception as _dep_err:
    logger.warning(f"QueueManager set_dependencies at startup failed: {_dep_err}")

@app.post("/api/connect")
async def connect_ibkr(config: ConnectionConfig, user: Optional[str] = Depends(get_current_user)):
    """连接到IBKR"""
    global ibkr_client
    
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: connection_manager.disconnect())
        connection_manager.update_config(config)
        success = await loop.run_in_executor(None, lambda: connection_manager.connect())
        
        if success:
            # 建立/更新 ibkr_client 引用
            if ibkr_client is None:
                ibkr_client_local = IBKRClient(connection_manager)
            else:
                ibkr_client_local = ibkr_client
            
            # 更新 risk_manager 的 ibkr_client（RiskManager 依赖 ibkr_client）
            try:
                risk_manager.ibkr_client = ibkr_client_local
                # 风险管理启动
                risk_manager.start()
            except Exception as re_err:
                logger.warning(f"RiskManager start warning: {re_err}")
            
            # 将 ibkr_client 注入到队列管理器
            try:
                queue_manager.set_dependencies(
                    ibkr_client=ibkr_client_local,
                    risk_manager=risk_manager,
                    strategy_manager=strategy_manager,
                    connection_manager=connection_manager,
                    websocket_manager=websocket_manager,
                    order_manager=order_manager
                )
            except Exception as dep_err:
                logger.warning(f"QueueManager set_dependencies warning: {dep_err}")
            
            # 赋值全局 ibkr_client
            ibkr_client = ibkr_client_local
            
            return {"success": True, "message": "Connected successfully"}
        else:
            raise HTTPException(status_code=400, detail="Failed to connect to IBKR")
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Connection error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/disconnect")
async def disconnect_ibkr(user: Optional[str] = Depends(get_current_user)):
    """断开IBKR连接"""
    global ibkr_client, risk_manager
    
    try:
        if risk_manager:
            risk_manager.stop()
        
        connection_manager.disconnect()
        
        return {"success": True, "message": "Disconnected successfully"}
        
    except Exception as e:
        logger.error(f"Disconnection error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/connection_status")
async def get_connection_status(user: Optional[str] = Depends(get_current_user)):
    """获取连接状态"""
    return {
        "connected": connection_manager.is_connected(),
        "account_type": "Paper" if connection_manager.config.paper_trading else "Live"
    }

# Market data endpoints
@app.post("/api/historical_data")
async def get_historical_data(request: HistoricalDataRequest, user: Optional[str] = Depends(get_current_user)):
    """获取历史数据"""
    if not ibkr_client or not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    
    try:
        # Run in executor to avoid blocking event loop
        loop = asyncio.get_event_loop()
        df = await loop.run_in_executor(
            None, 
            lambda: ibkr_client.get_historical_data(
                symbol=request.symbol,
                duration=request.duration,
                bar_size=request.bar_size,
                exchange=request.exchange
            )
        )
        
        if df.empty:
            raise HTTPException(status_code=404, detail="No data found")
        
        # 保存到数据库
        db_manager = get_db_manager()
        db_manager.save_historical_data(request.symbol, df, bar_size=request.bar_size)
        
        # 可选：同时保存CSV作为备份
        filename = f"data/{request.symbol}_historical.csv"
        try:
            os.makedirs(os.path.dirname(filename), exist_ok=True)
            df.to_csv(filename, index=False)
        except Exception as e:
            logger.warning(f"Failed to write CSV backup for historical data: {e}")
        
        return {
            "success": True,
            "data": df.to_dict('records'),
            "filename": filename,
            "rows": len(df)
        }
        
    except Exception as e:
        logger.error(f"Historical data error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/chart_data")
async def get_chart_data(request: ChartDataRequest, user: Optional[str] = Depends(get_current_user)):
    """获取图表数据"""
    if not ibkr_client or not ibkr_client.connected:
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    
    try:
        # Run in executor to avoid blocking event loop
        loop = asyncio.get_event_loop()
        df = await loop.run_in_executor(
            None,
            lambda: ibkr_client.get_historical_data(
                symbol=request.symbol,
                duration=request.duration,
                bar_size=request.bar_size,
                exchange=request.exchange
            )
        )
        
        if df.empty:
            raise HTTPException(status_code=404, detail="No chart data found")
        
        # Format data for chart display
        chart_data = []
        for _, row in df.iterrows():
            chart_data.append({
                "time": row['date'].isoformat() if hasattr(row['date'], 'isoformat') else str(row['date']),
                "open": float(row['open']),
                "high": float(row['high']),
                "low": float(row['low']),
                "close": float(row['close']),
                "volume": int(row['volume']) if 'volume' in row else 0
            })
        
        return {
            "success": True,
            "data": chart_data,
            "symbol": request.symbol,
            "duration": request.duration,
            "bar_size": request.bar_size
        }
        
    except Exception as e:
        logger.error(f"Chart data error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/realtime_data/{symbol}")
async def get_realtime_data(symbol: str, exchange: str = "SMART", user: Optional[str] = Depends(get_current_user)):
    """获取实时数据"""
    if not ibkr_client or not ibkr_client.connected:
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    
    try:
        # Run in executor with timeout to avoid blocking event loop
        loop = asyncio.get_event_loop()
        try:
            data = await asyncio.wait_for(
                loop.run_in_executor(
                    None, 
                    lambda: ibkr_client.get_real_time_data(symbol, exchange)
                ), 
                timeout=10.0  # 10-second timeout
            )
        except asyncio.TimeoutError:
            logger.warning(f"Real-time data request for {symbol} timed out, checking cache")
            # Try to get cached data as fallback
            cached_data = None
            if hasattr(ibkr_client, 'price_cache') and symbol in ibkr_client.price_cache:
                cached_price = ibkr_client.price_cache.get(symbol)
                if cached_price:
                    cached_data = {
                        'symbol': symbol,
                        'last': cached_price,
                        'bid': 0,
                        'ask': 0,
                        'volume': 0,
                        'timestamp': datetime.now().isoformat(),
                        'is_cached': True
                    }
            
            if cached_data:
                logger.info(f"Returning cached data for {symbol}: {cached_price}")
                return {"success": True, "data": cached_data}
            else:
                logger.warning(f"No cached data available for {symbol}")
                return {"success": False, "error": "Real-time data request timed out and no cached data available"}
        
        if data:
            return {"success": True, "data": data}
        else:
            # Try cached data as fallback when no data returned
            if hasattr(ibkr_client, 'price_cache') and symbol in ibkr_client.price_cache:
                cached_price = ibkr_client.price_cache.get(symbol)
                if cached_price:
                    cached_data = {
                        'symbol': symbol,
                        'last': cached_price,
                        'bid': 0,
                        'ask': 0,
                        'volume': 0,
                        'timestamp': datetime.now().isoformat(),
                        'is_cached': True
                    }
                    logger.info(f"No fresh data for {symbol}, returning cached: {cached_price}")
                    return {"success": True, "data": cached_data}
            
            raise HTTPException(status_code=404, detail="No data available")
            
    except Exception as e:
        logger.error(f"Real-time data error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Trading endpoints
@app.post("/api/place_order")
async def place_order(order: OrderRequest, user: Optional[str] = Depends(get_current_user)):
    """下单"""
    if not ibkr_client or not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    
    try:
        # Run blocking order placement in executor
        loop = asyncio.get_event_loop()
        
        if order.order_type.upper() == "MARKET":
            trade = await loop.run_in_executor(
                None,
                lambda: ibkr_client.place_market_order(
                    symbol=order.symbol,
                    quantity=order.quantity,
                    action=order.action,
                    exchange=order.exchange,
                    sec_type=order.sec_type
                )
            )
        elif order.order_type.upper() == "LIMIT":
            if not order.price:
                raise HTTPException(status_code=400, detail="Price required for limit order")
            trade = await loop.run_in_executor(
                None,
                lambda: ibkr_client.place_limit_order(
                    symbol=order.symbol,
                    quantity=order.quantity,
                    price=order.price,
                    action=order.action,
                    exchange=order.exchange,
                    sec_type=order.sec_type
                )
            )
        else:
            raise HTTPException(status_code=400, detail="Invalid order type")
        
        if trade:
            # 使用 OrderManager 记录订单
            try:
                if 'order_manager' in globals() and order_manager:
                    normalized_order_type = "MKT" if order.order_type.upper() == "MARKET" else "LMT"
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
                        notes=(f"submitted_by={user}" if user else None)
                    )
                else:
                    logger.warning("OrderManager is not initialized; order will not be recorded in OrderManager")
            except Exception as rec_err:
                logger.warning(f"Failed to record order in OrderManager: {rec_err}")
            
            await websocket_manager.broadcast({  # 替换 manager
                "type": "order_placed",
                "order_id": trade.order.orderId,
                "symbol": order.symbol,
                "action": order.action,
                "quantity": order.quantity
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

@app.delete("/api/cancel_order/{order_id}")
async def cancel_order(order_id: int, user: Optional[str] = Depends(get_current_user)):
    """取消订单"""
    if not ibkr_client or not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    
    try:
        # Run in executor to avoid blocking event loop
        loop = asyncio.get_event_loop()
        success, message = await loop.run_in_executor(
            None,
            lambda: ibkr_client.cancel_order(order_id)
        )
        
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
            
    except Exception as e:
        logger.error(f"Order cancellation error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/cancel_order")
async def cancel_order_post(request: dict, user: Optional[str] = Depends(get_current_user)):
    """取消订单 (POST方法)"""
    if not ibkr_client or not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    
    try:
        order_id = request.get('order_id')
        if not order_id:
            raise HTTPException(status_code=400, detail="order_id is required")
            
        # Run in executor to avoid blocking event loop
        loop = asyncio.get_event_loop()
        success, message = await loop.run_in_executor(
            None,
            lambda: ibkr_client.cancel_order(int(order_id))
        )
        
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

@app.delete("/api/cancel_all_orders")
async def cancel_all_orders(user: Optional[str] = Depends(get_current_user)):
    """取消所有订单"""
    if not ibkr_client or not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    
    try:
        # Run in executor to avoid blocking event loop
        loop = asyncio.get_event_loop()
        cancelled_count = await loop.run_in_executor(
            None,
            ibkr_client.cancel_all_orders
        )
        
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

@app.get("/api/orders")
async def get_orders(user: Optional[str] = Depends(get_current_user)):
    """获取订单列表（从数据库读取，包含历史订单）"""
    try:
        # 先从 IB 同步最新订单状态到本地记录（若已连接）
        try:
            if ibkr_client and connection_manager.is_connected():
                # Run in executor to avoid blocking event loop with a short timeout
                loop = asyncio.get_event_loop()
                await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        # 调用 ibkr_client.get_orders 刷新 IBKR 侧状态并同步到 OrderManager
                        lambda: ibkr_client.get_orders(risk_manager, strategy_manager, order_manager)
                    ),
                    timeout=3.0
                )
        except Exception as sync_err:
            logger.debug(f"Order sync skipped: {sync_err}")

        if 'order_manager' not in globals() or not order_manager:
            raise HTTPException(status_code=500, detail="Order manager not initialized")
        
        # 从 OrderManager 获取所有订单数据（已按创建时间降序排列）
        orders = order_manager.get_orders()
        
        # 转换字段名以匹配前端期望
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
            
            # 计算触发类型：仅对机器人订单（auto）有效
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
                'trigger_type': trigger_type
            }
            converted_orders.append(converted_order)
        
        return {"success": True, "orders": converted_orders}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Get orders error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Account endpoints
@app.get("/api/account_info")
async def get_account_info(user: Optional[str] = Depends(get_current_user)):
    """获取账户信息"""
    if not ibkr_client or not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    
    try:
        # Run in executor with timeout to avoid blocking event loop
        loop = asyncio.get_event_loop()
        try:
            info = await asyncio.wait_for(
                loop.run_in_executor(None, ibkr_client.get_account_info),
                timeout=10.0  # 10-second timeout
            )
        except asyncio.TimeoutError:
            logger.warning("Account info request timed out")
            return {"success": False, "error": "Account info request timed out"}
            
        return {"success": True, "account_info": info}
        
    except Exception as e:
        logger.error(f"Get account info error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/positions")
async def get_positions(user: Optional[str] = Depends(get_current_user)):
    """获取持仓列表"""
    if not ibkr_client or not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    
    try:
        # Run in executor with timeout to avoid blocking event loop
        loop = asyncio.get_event_loop()
        try:
            positions = await asyncio.wait_for(
                loop.run_in_executor(None, ibkr_client.get_positions),
                timeout=10.0  # 10-second timeout
            )
        except asyncio.TimeoutError:
            logger.warning("Positions request timed out")
            return {"success": False, "error": "Positions request timed out"}
            
        return {"success": True, "positions": positions}
        
    except Exception as e:
        logger.error(f"Get positions error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Risk management endpoints
@app.post("/api/risk_rules")
async def add_risk_rule(rule: RiskRuleRequest, user: Optional[str] = Depends(get_current_user)):
    """添加风险管理规则"""
    if not risk_manager:
        raise HTTPException(status_code=400, detail="Risk manager not initialized")
    
    try:
        logger.info(f"API add_risk_rule: symbol={rule.symbol}, SL={rule.stop_loss_price}, TP={rule.take_profit_price}, max_loss%={rule.max_loss_percent}")
        risk_manager.add_risk_rule(
            symbol=rule.symbol,
            stop_loss_price=rule.stop_loss_price,
            take_profit_price=rule.take_profit_price,
            max_loss_percent=rule.max_loss_percent
        )
        
        return {"success": True, "message": "Risk rule added successfully"}
        
    except Exception as e:
        logger.error(f"Add risk rule error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/risk_rules")
async def get_risk_rules(user: Optional[str] = Depends(get_current_user)):
    """获取风险管理规则"""
    if not risk_manager:
        raise HTTPException(status_code=400, detail="Risk manager not initialized")
    
    try:
        rules = risk_manager.get_risk_rules()
        # Convert to serializable format
        serializable_rules = {}
        for symbol, rule in rules.items():
            serializable_rules[symbol] = {
                "symbol": rule.symbol,
                "stop_loss_price": rule.stop_loss_price,
                "take_profit_price": rule.take_profit_price,
                "max_loss_percent": rule.max_loss_percent,
                "enabled": rule.enabled,
                "created_at": rule.created_at.isoformat()
            }
        
        return {"success": True, "rules": serializable_rules}
        
    except Exception as e:
        logger.error(f"Get risk rules error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/risk_rules/{symbol}")
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

@app.delete("/api/risk_rules/{symbol}")
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

@app.get("/api/order_queue_status")
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

@app.get("/api/risk-config")
async def get_risk_config(user: Optional[str] = Depends(get_current_user)):
    """Get risk management configuration from environment variables"""
    try:
        stop_loss_ratio = float(os.getenv('STOP_LOSS_RATIO', '0.05'))
        take_profit_ratio = float(os.getenv('TAKE_PROFIT_RATIO', '0.05'))
        
        return {
            "stop_loss_ratio": stop_loss_ratio,
            "take_profit_ratio": take_profit_ratio
        }
    except Exception as e:
        logger.error(f"Get risk config error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/auto_orders")
async def get_auto_orders(user: Optional[str] = Depends(get_current_user)):
    """获取自动订单记录（只返回自动订单，不包含手动订单）"""
    if not risk_manager:
        raise HTTPException(status_code=400, detail="Risk manager not initialized")
    
    try:
        all_orders = risk_manager.get_auto_orders()
        # 只返回自动订单，过滤掉手动订单
        auto_orders = [order for order in all_orders if order.get('order_source') == 'auto']
        return {"success": True, "orders": auto_orders}
        
    except Exception as e:
        logger.error(f"Get auto orders error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# WebSocket endpoint
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket_manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            # Handle incoming WebSocket messages if needed
            message = json.loads(data)
            
            if message.get("type") == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
            
    except WebSocketDisconnect:
        websocket_manager.disconnect(websocket)

# Strategy APIs
@app.get("/api/strategy/settings")
async def get_strategy_settings(user: Optional[str] = Depends(get_current_user)):
    if not strategy_manager:
        raise HTTPException(status_code=500, detail="Strategy manager not initialized")
    return {"success": True, "settings": strategy_manager.get_settings()}

@app.post("/api/strategy/settings")
async def update_strategy_settings(settings: StrategySettingsRequest, user: Optional[str] = Depends(get_current_user)):
    if not strategy_manager:
        raise HTTPException(status_code=500, detail="Strategy manager not initialized")
    strategy_manager.update_settings(settings.model_dump())
    return {"success": True}

# --- Strategy Auth Token Generation Endpoint ---
from fastapi import Header

@app.post("/api/strategy/settings/generate-token")
async def generate_strategy_token(user: Optional[str] = Depends(get_current_user)):
    token = strategy_manager.generate_auth_token()
    return {"success": True, "token": token}

@app.get("/api/strategies")
async def list_strategies(user: Optional[str] = Depends(get_current_user)):
    if not strategy_manager:
        raise HTTPException(status_code=500, detail="Strategy manager not initialized")
    return {"success": True, "strategies": strategy_manager.get_strategies()}

@app.post("/api/strategies")
async def add_strategy_api(strategy: StrategyRequest, user: Optional[str] = Depends(get_current_user)):
    if not strategy_manager:
        raise HTTPException(status_code=500, detail="Strategy manager not initialized")
    success = strategy_manager.add_strategy(strategy.model_dump())
    if not success:
        raise HTTPException(status_code=400, detail="Failed to add strategy")
    return {"success": True}

@app.get("/api/strategies/{name}")
async def get_strategy_api(name: str, user: Optional[str] = Depends(get_current_user)):
    if not strategy_manager:
        raise HTTPException(status_code=500, detail="Strategy manager not initialized")
    strategy = strategy_manager.get_strategy(name)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return {"success": True, "strategy": strategy}

@app.put("/api/strategies/{name}")
async def update_strategy_api(name: str, updates: Dict[str, Any], user: Optional[str] = Depends(get_current_user)):
    if not strategy_manager:
        raise HTTPException(status_code=500, detail="Strategy manager not initialized")
    success = strategy_manager.update_strategy(name, updates)
    if not success:
        raise HTTPException(status_code=400, detail="Failed to update strategy")
    return {"success": True}

@app.delete("/api/strategies/{name}")
async def delete_strategy_api(name: str, user: Optional[str] = Depends(get_current_user)):
    if not strategy_manager:
        raise HTTPException(status_code=500, detail="Strategy manager not initialized")
    success = strategy_manager.delete_strategy(name)
    if not success:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return {"success": True}

@app.post("/api/strategies/{name}/toggle")
async def toggle_strategy_api(name: str, enabled: bool, user: Optional[str] = Depends(get_current_user)):
    if not strategy_manager:
        raise HTTPException(status_code=500, detail="Strategy manager not initialized")
    success = strategy_manager.update_strategy(name, {"enabled": enabled})
    if not success:
        raise HTTPException(status_code=400, detail="Failed to toggle strategy")
    return {"success": True}

@app.get("/api/strategies/{name}/performance")
async def get_strategy_performance_api(name: str, range: str = "1d", start: Optional[str] = None, end: Optional[str] = None, user: Optional[str] = Depends(get_current_user)):
    if not strategy_manager:
        raise HTTPException(status_code=500, detail="Strategy manager not initialized")
    
    start_date = None
    end_date = None
    if start:
        start_date = datetime.fromisoformat(start)
    if end:
        end_date = datetime.fromisoformat(end)
    
    # Quick ranges
    now = datetime.now()
    if range == "1d":
        start_date = now - timedelta(days=1)
    elif range == "1w":
        start_date = now - timedelta(weeks=1)
    elif range == "1m":
        start_date = now - timedelta(days=30)
    elif range == "wtd":
        start_date = now - timedelta(days=now.weekday())
    elif range == "mtd":
        start_date = now.replace(day=1)
    elif range == "ytd":
        start_date = now.replace(month=1, day=1)
    
    metrics = strategy_manager.get_strategy_performance(name, start_date, end_date)
    return {"success": True, "metrics": metrics}

# External trigger endpoint - Async queue version
@app.post("/api/strategy")
async def external_strategy_order(payload: ExternalStrategyOrderRequest, req: Request):
    """策略订单接口 - 立即返回200，异步处理"""
    if not strategy_manager:
        raise HTTPException(status_code=500, detail="Strategy manager not initialized")
    
    # Auth check - strategy interface uses token in body or headers when enabled
    # Do this check FIRST to avoid any IBKR calls for unauthorized requests
    settings = strategy_manager.get_settings()
    if settings.get('enable_auth', False):
        # 1) Prefer body token
        token = payload.token
        # 2) Fallback to Authorization: Bearer <token>
        if not token:
            auth_header = req.headers.get('Authorization')
            if auth_header and auth_header.startswith('Bearer '):
                token = auth_header.split(' ', 1)[1]
        # 3) Fallback to X-Strategy-Token header
        if not token:
            token = req.headers.get('X-Strategy-Token')
        if not token or not strategy_manager.validate_auth_token(token):
            raise HTTPException(status_code=401, detail="Unauthorized")
    
    # Validate strategy exists and enabled - also before IBKR calls
    strategy = strategy_manager.get_strategy(payload.strategy)
    if not strategy or not strategy.get('enabled', False):
        raise HTTPException(status_code=400, detail="Strategy disabled or not found")
    
    # 基本参数验证
    if payload.type.upper() == "LIMIT" and not payload.price:
        raise HTTPException(status_code=400, detail="Price required for limit order")
    
    if payload.type.upper() not in ["MARKET", "LIMIT"]:
        raise HTTPException(status_code=400, detail="Invalid order type")
    
    # 将任务加入队列
    task_payload = {
        "strategy": payload.strategy,
        "symbol": payload.symbol,
        "action": payload.action,
        "qty": payload.qty,
        "type": payload.type,
        "price": payload.price,
        "exchange": payload.exchange,
        "sec_type": payload.sec_type
    }
    
    task_id = queue_manager.add_task("strategy_order", task_payload)
    
    # 立即返回200
    return {
        "success": True,
        "task_id": task_id,
        "message": "Strategy order queued for processing",
        "status": "queued"
    }

# Queue Management APIs
@app.get("/api/queue/status")
async def get_queue_status():
    """获取队列整体状态"""
    if not queue_manager:
        raise HTTPException(status_code=500, detail="Queue manager not initialized")
    
    status = queue_manager.get_queue_status()
    return status

@app.get("/api/queue/task/{task_id}")
async def get_task_status(task_id: str):
    """获取特定任务状态"""
    if not queue_manager:
        raise HTTPException(status_code=500, detail="Queue manager not initialized")
    
    task_status = queue_manager.get_task_status(task_id)
    if not task_status:
        raise HTTPException(status_code=404, detail="Task not found")
    
    return task_status

@app.get("/api/queue/tasks")
async def get_all_tasks():
    """获取所有任务列表"""
    if not queue_manager:
        raise HTTPException(status_code=500, detail="Queue manager not initialized")
    
    tasks = queue_manager.get_all_tasks()
    return {"tasks": tasks}

# Strategy Executions API
@app.get("/api/strategies/{name}/executions")
async def get_strategy_executions(name: str, limit: int = 100, user: Optional[str] = Depends(get_current_user)):
    """Get strategy execution logs"""
    if not strategy_manager:
        raise HTTPException(status_code=500, detail="Strategy manager not initialized")
    try:
        executions = strategy_manager.get_executions(name, limit)
        return {"success": True, "executions": [asdict(exec) if hasattr(exec, '__dict__') else exec for exec in executions]}
    except Exception as e:
        logger.error(f"Get strategy executions error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Clear all executions for a strategy
@app.delete("/api/strategies/{name}/executions")
async def clear_strategy_executions(name: str, user: Optional[str] = Depends(get_current_user)):
    """Clear all executions for a strategy"""
    try:
        db = get_db_manager()
        deleted = db.delete_strategy_executions(name)
        # 同步清理内存中的 executions
        if strategy_manager:
            before = len(strategy_manager.executions)
            strategy_manager.executions = [e for e in strategy_manager.executions if e.strategy_name != name]
            after = len(strategy_manager.executions)
            logger.info(f"Cleared in-memory executions for {name}: {before - after} removed")
        return {"success": True, "deleted": int(deleted)}
    except Exception as e:
        logger.error(f"Clear strategy executions error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# System Logs API (kept for general debugging)
@app.get("/api/logs")
async def get_logs(lines: int = 100, level: str = "all", user: Optional[str] = Depends(get_current_user)):
    try:
        log_file = "logs/trading_bot.log"
        if not os.path.exists(log_file):
            return {"success": True, "logs": []}
        with open(log_file, 'r') as f:
            content = f.readlines()
        if level.lower() != "all":
            content = [line for line in content if f"| {level.upper()} |" in line]
        return {"success": True, "logs": content[-lines:]}
    except Exception as e:
        logger.error(f"Get logs error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def main():
    """Main application entry point"""
    print_banner()
    
    # Setup
    setup_logging()
    create_directories()
    
    logger.info("Starting IBKR Trading Bot...")
    
    # Check environment
    if not check_environment():
        logger.error("Environment check failed. Please configure .env file.")
        return
    
    # Auto connect if enabled in .env
    auto_connect = os.getenv('AUTO_CONNECT', 'False').lower() == 'true'
    if auto_connect:
        logger.info("Auto-connect enabled, attempting to connect to IBKR...")
        try:
            # Use the connection manager directly for synchronous connection test
            success = connection_manager.connect()
            if not success:
                logger.warning("IBKR auto-connection failed. Continuing without connection...")
                logger.info("You can connect later through the web interface.")
            else:
                logger.info("IBKR auto-connection successful!")
        except Exception as e:
            logger.warning(f"Auto-connection failed: {e}")
            logger.info("You can connect later through the web interface.")
    else:
        logger.info("Auto-connect disabled. You can connect later through the web interface.")
    
    # Start web server
    logger.info("Starting web server...")
    logger.info("Web UI available at: http://localhost:8000")
    logger.info("Press Ctrl+C to stop the application")
    
    try:
        import uvicorn
        uvicorn.run(
            app,
            host=os.getenv('WEB_HOST', '0.0.0.0'),
            port=int(os.getenv('WEB_PORT', '8000')),
            log_level="warning"  # 减少HTTP请求日志显示
        )
        
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    except Exception as e:
        logger.error(f"Application error: {e}")
    finally:
        logger.info("Application stopped")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nApplication interrupted by user")
    except Exception as e:
        print(f"Fatal error: {e}")
        sys.exit(1)