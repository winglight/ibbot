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

# Global variables
load_dotenv()

# FastAPI app
app = FastAPI(title="IBKR Trading Bot", version="1.0.0")

# Auth config
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'change_me')
JWT_SECRET = os.getenv('JWT_SECRET', 'please_change_me')
JWT_ALGORITHM = os.getenv('JWT_ALGORITHM', 'HS256')
JWT_EXPIRE_DAYS = int(os.getenv('JWT_EXPIRE_DAYS', '1'))

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
telegram_notifier = None
risk_manager = None
strategy_manager = None
websocket_connections = []

# Pydantic models
class OrderRequest(BaseModel):
    symbol: str
    quantity: int
    action: str  # BUY or SELL
    order_type: str  # MARKET or LIMIT
    price: Optional[float] = None
    exchange: str = "SMART"
    sec_type: str = "STK"  # STK for stocks, FUT for futures

class RiskRuleRequest(BaseModel):
    symbol: str
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    max_loss_percent: Optional[float] = None

class HistoricalDataRequest(BaseModel):
    symbol: str
    duration: str = "1 Y"
    bar_size: str = "1 day"
    exchange: str = "SMART"

class ChartDataRequest(BaseModel):
    symbol: str
    duration: str = "1 D"
    bar_size: str = "5 mins"
    exchange: str = "SMART"

# Strategy related models
class StrategyRequest(BaseModel):
    name: str
    strategy_type: str  # "external" or "internal"
    enabled: bool = True
    api_endpoint: Optional[str] = None
    file_path: Optional[str] = None
    description: Optional[str] = None
    use_template: bool = False

class StrategySettingsRequest(BaseModel):
    api_endpoint: str = "/api/strategy"
    api_method: str = "POST"
    api_payload_template: Dict[str, Any] = {
        "strategy": "{{strategy_name}}",
        "symbol": "{{symbol}}",
        "action": "{{action}}",
        "qty": "{{quantity}}",
        "type": "{{order_type}}",
        "price": "{{price}}"
    }
    strategy_path: str = "strategy/"
    enable_auth: bool = False
    auth_token: Optional[str] = None

class ExternalStrategyOrderRequest(BaseModel):
    strategy: str
    symbol: str
    action: str  # BUY or SELL
    qty: int
    type: str  # MARKET or LIMIT
    price: Optional[float] = None
    exchange: str = "SMART"
    sec_type: str = "STK"
    token: Optional[str] = None  # Optional auth token for strategy auth

# WebSocket connection manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
    
    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
    
    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)
    
    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_text(json.dumps(message))
            except:
                pass

manager = ConnectionManager()

def setup_logging():
    """Setup logging configuration"""
    # Remove default logger
    logger.remove()
    
    # Add console logger
    logger.add(
        sys.stdout,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level="INFO"
    )
    
    # Add file logger
    logger.add(
        "logs/trading_bot.log",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
        level="DEBUG",
        rotation="1 day",
        retention="30 days",
        compression="zip"
    )
    
    logger.info("Logging setup completed")

def create_directories():
    """Create necessary directories if they don't exist"""
    directories = ['logs', 'data', 'strategy']
    for directory in directories:
        Path(directory).mkdir(exist_ok=True)
        logger.info(f"Directory '{directory}' ensured")

def check_environment():
    """Check if all required environment variables are set"""
    required_vars = [
        'IBKR_HOST',
        'IBKR_PORT',
        'IBKR_CLIENT_ID'
    ]
    
    optional_vars = [
        'TELEGRAM_BOT_TOKEN',
        'TELEGRAM_CHAT_ID',
        'LOSS_THRESHOLD_1',
        'LOSS_THRESHOLD_2',
        'CHECK_INTERVAL'
    ]
    
    missing_vars = []
    for var in required_vars:
        if not os.getenv(var):
            missing_vars.append(var)
    
    if missing_vars:
        logger.warning(f"Missing required environment variables: {missing_vars}")
        logger.info("Please copy .env.example to .env and configure the settings")
    
    # Check optional variables
    missing_optional = []
    for var in optional_vars:
        if not os.getenv(var):
            missing_optional.append(var)
    
    if missing_optional:
        logger.info(f"Optional environment variables not set: {missing_optional}")
        logger.info("Some features may not be available")
    
    return len(missing_vars) == 0

async def test_connection_async():
    """Test IBKR connection without blocking event loop"""
    logger.info("Testing IBKR connection...")
    local_client = IBKRClient()
    try:
        loop = asyncio.get_event_loop()
        success = await loop.run_in_executor(None, local_client.connect)
        if success:
            logger.info("IBKR connection test successful")
            account_info = await loop.run_in_executor(None, local_client.get_account_info)
            if account_info:
                logger.info(f"Account ID: {account_info.get('account_id', 'N/A')}")
                logger.info(f"Account Type: {account_info.get('account_type', 'N/A')}")
            await loop.run_in_executor(None, local_client.disconnect)
            return True
        else:
            logger.error("IBKR connection test failed")
            return False
    except Exception as e:
        logger.error(f"Connection test failed: {e}")
        return False

def print_banner():
    """Print application banner"""
    banner = """
    ╔══════════════════════════════════════════════════════════════╗
    ║                    IBKR Trading Bot v1.0                    ║
    ║                                                              ║
    ║  Features:                                                   ║
    ║  • Real-time market data                                     ║
    ║  • Automated trading (Market & Limit orders)                ║
    ║  • Risk management with stop-loss/take-profit               ║
    ║  • Telegram notifications                                    ║
    ║  • Web-based user interface                                 ║
    ║  • Historical data export                                   ║
    ║                                                              ║
    ║  Web UI: http://localhost:8000                               ║
    ╚══════════════════════════════════════════════════════════════╝
    """
    print(banner)

# FastAPI event handlers
@app.on_event("startup")
async def startup_event():
    global ibkr_client, telegram_notifier, risk_manager, strategy_manager
    # Initialize components
    ibkr_client = IBKRClient()
    telegram_notifier = TelegramNotifier()
    risk_manager = RiskManager(ibkr_client, telegram_notifier)
    strategy_manager = StrategyManager(data_dir="data", strategy_dir="strategy")
    
    # Start queue worker
    await queue_manager.start_worker()
    logger.info("Queue worker started")
    
    # Use async test to avoid blocking
    ok = await test_connection_async()
    if not ok:
        logger.error("Environment setup failed. Please check your configuration.")
        return
    
    # Auto connect if enabled in .env
    auto_connect = os.getenv('AUTO_CONNECT', 'False').lower() == 'true'
    if auto_connect:
        logger.info("Auto-connect enabled, attempting to connect to IBKR...")
        try:
            # Use thread pool to avoid event loop conflicts, with proper error handling
            loop = asyncio.get_event_loop()
            success = await loop.run_in_executor(None, lambda: connection_manager.connect())
            if success:
                logger.info("Auto-connected to IBKR successfully")
                # Start risk manager
                risk_manager.start()
            else:
                logger.warning("Failed to auto-connect to IBKR")
        except Exception as e:
            logger.error(f"Auto-connection error: {e}")
    
    logger.info("Web API started")

@app.on_event("shutdown")
async def shutdown_event():
    global ibkr_client, risk_manager
    
    # Stop queue worker
    await queue_manager.stop_worker()
    logger.info("Queue worker stopped")
    
    if risk_manager:
        risk_manager.stop()
    
    if ibkr_client and connection_manager.is_connected():
        ibkr_client.disconnect()
    
    logger.info("Web API shutdown")

# Static files
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse)
async def read_root():
    """返回主页面"""
    return FileResponse("static/index.html")

# Connection endpoints
@app.post("/api/connect")
async def connect_ibkr(config: ConnectionConfig, user: Optional[str] = Depends(get_current_user)):
    """连接到IBKR"""
    global ibkr_client
    
    try:
        # 使用连接管理器更新配置并连接
        connection_manager.update_config(config)
        
        # 在线程池中执行同步连接操作，避免事件循环冲突
        loop = asyncio.get_event_loop()
        success = await loop.run_in_executor(None, lambda: connection_manager.connect())
        
        if success:
            # Start risk manager
            risk_manager.start()
            
            return {"success": True, "message": "Connected successfully"}
        else:
            raise HTTPException(status_code=400, detail="Failed to connect to IBKR")
            
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
            # Record manual order if risk_manager is available
            if risk_manager:
                from src.risk_manager import AutoOrder
                from datetime import datetime
                import os
                
                # Normalize order type to MKT/LMT format
                normalized_order_type = "MKT" if order.order_type.upper() == "MARKET" else "LMT"
                
                auto_order = AutoOrder(
                    order_id=str(trade.order.orderId),
                    symbol=order.symbol,
                    action=order.action,
                    quantity=order.quantity,
                    order_type=normalized_order_type,
                    price=order.price if order.price else 0.0,
                    status="Submitted",
                    created_at=datetime.now(),
                    executed_at=None,
                    rule_id="manual",
                    order_source=user or "manual"
                )
                
                risk_manager.auto_orders[str(trade.order.orderId)] = auto_order
                # 保存到数据库，保证持久化
                risk_manager.save_auto_order_to_db(auto_order)
                
                await manager.broadcast({
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
            await manager.broadcast({
                "type": "order_cancelled",
                "order_id": order_id
            })
            # 同步更新本地记录
            if risk_manager:
                risk_manager.update_order_status(str(order_id), "Cancelled")
            
            return {"success": True, "message": message}
        else:
            # 如果返回"not found or already completed"，则删除本地记录
            if "not found" in message.lower():
                if risk_manager:
                    removed = risk_manager.remove_order_record(str(order_id))
                    if removed:
                        return {"success": True, "message": "Order not found on IBKR; removed from local records"}
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
            await manager.broadcast({
                "type": "order_cancelled",
                "order_id": order_id
            })
            # 同步更新本地记录
            if risk_manager:
                risk_manager.update_order_status(str(order_id), "Cancelled")
            
            return {"success": True, "message": message}
        else:
            # 如果返回"not found or already completed"，则删除本地记录
            if "not found" in message.lower():
                if risk_manager:
                    removed = risk_manager.remove_order_record(str(order_id))
                    if removed:
                        return {"success": True, "message": "Order not found on IBKR; removed from local records"}
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
        
        await manager.broadcast({
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
    if not risk_manager:
        raise HTTPException(status_code=400, detail="Risk manager not initialized")
    
    try:
        # 先从 IB 同步最新订单状态到本地记录（若已连接）
        try:
            if ibkr_client and connection_manager.is_connected():
                # Run in executor to avoid blocking event loop with a short timeout
                loop = asyncio.get_event_loop()
                await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        lambda: ibkr_client.get_orders(risk_manager, strategy_manager)
                    ),
                    timeout=3.0
                )
        except Exception as sync_err:
            logger.debug(f"Order sync skipped: {sync_err}")
        
        # 从 risk_manager 获取所有订单数据（从数据库加载）
        orders = risk_manager.get_auto_orders()
        
        # 按创建时间反序排列（最新的在前）
        orders.sort(key=lambda x: x.get('created_at', ''), reverse=True)
        
        # 转换字段名以匹配前端期望
        converted_orders = []
        for order in orders:
            raw_type = (order.get('order_type') or '').upper()
            if raw_type in ('MARKET', 'MKT'):
                order_type = 'MKT'
            elif raw_type in ('LIMIT', 'LMT'):
                order_type = 'LMT'
            else:
                order_type = raw_type or 'MKT'
            
            price = order.get('price')
            try:
                price_val = float(price) if price is not None and price != '' else 0.0
            except Exception:
                price_val = 0.0
            
            # 计算触发类型：仅对机器人订单（auto）有效
            trigger_type = None
            try:
                if order.get('order_source') == 'auto':
                    rule_id = order.get('rule_id')
                    symbol = order.get('symbol')
                    # 约定：风控触发时 rule_id == symbol；策略触发时 rule_id 为策略名（通常 != symbol）
                    if rule_id and rule_id != symbol:
                        trigger_type = 'strategy'
                    else:
                        trigger_type = 'risk_rule'
            except Exception:
                trigger_type = None
            
            converted_order = {
                'order_id': order['order_id'],
                'symbol': order['symbol'],
                'action': order['action'],
                'quantity': order['quantity'],
                'order_type': order_type,
                'limit_price': price_val if order_type == 'LMT' and price_val > 0 else None,
                'avg_fill_price': price_val if order_type == 'MKT' and price_val > 0 else 0,
                'status': order['status'],
                'filled': order['quantity'] if order['status'] == 'Filled' else 0,
                'remaining': 0 if order['status'] == 'Filled' else order['quantity'],
                'submit_time': order.get('created_at'),
                'order_source': order.get('order_source', 'manual'),
                'trigger_type': trigger_type
            }
            converted_orders.append(converted_order)
        
        return {"success": True, "orders": converted_orders}
        
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
            raise HTTPException(status_code=404, detail=f"Risk rule for {symbol} not found")
        
        return {"success": True, "message": f"Risk rule for {symbol} {'enabled' if enabled else 'disabled'} successfully"}
        
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
            "processing": risk_manager.processing_orders
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
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            # Handle incoming WebSocket messages if needed
            message = json.loads(data)
            
            if message.get("type") == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
            
    except WebSocketDisconnect:
        manager.disconnect(websocket)

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
            if not test_connection():
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
            log_level="info"
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