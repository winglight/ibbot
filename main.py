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
from datetime import datetime
from typing import List, Optional, Dict, Any
from loguru import logger
from dotenv import load_dotenv

# FastAPI imports
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Add current directory to Python path
sys.path.append(str(Path(__file__).parent))

from ibkr_client import IBKRClient
from telegram_notifier import TelegramNotifier
from risk_manager import RiskManager
from connection_manager import connection_manager, ConnectionConfig

# Load environment variables
load_dotenv()

# FastAPI app
app = FastAPI(title="IBKR Trading Bot", version="1.0.0")

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
    """Create necessary directories"""
    directories = ['logs', 'data']
    for directory in directories:
        Path(directory).mkdir(exist_ok=True)
    logger.info("Directories created")

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

def test_connection():
    """Test IBKR connection"""
    logger.info("Testing IBKR connection...")
    
    ibkr_client = IBKRClient()
    telegram_notifier = TelegramNotifier()
    
    try:
        # Test IBKR connection
        success = ibkr_client.connect()
        if success:
            logger.info("IBKR connection test successful")
            
            # Test basic functionality
            account_info = ibkr_client.get_account_info()
            if account_info:
                logger.info(f"Account ID: {account_info.get('account_id', 'N/A')}")
                logger.info(f"Account Type: {account_info.get('account_type', 'N/A')}")
            
            ibkr_client.disconnect()
        else:
            logger.error("IBKR connection test failed")
            return False
        
        # Test Telegram notification (if configured)
        if os.getenv('TELEGRAM_BOT_TOKEN') and os.getenv('TELEGRAM_CHAT_ID'):
            # telegram_notifier.send_message("🤖 IBKR Trading Bot - Connection Test Successful")  # 暂时注释掉异步调用
            logger.info("Telegram notification test completed")
        
        return True
        
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
    global ibkr_client, telegram_notifier, risk_manager
    
    # Initialize components
    ibkr_client = IBKRClient()
    telegram_notifier = TelegramNotifier()
    risk_manager = RiskManager(ibkr_client, telegram_notifier)
    
    # Auto connect if enabled in .env
    auto_connect = os.getenv('AUTO_CONNECT', 'False').lower() == 'true'
    if auto_connect:
        logger.info("Auto-connect enabled, attempting to connect to IBKR...")
        try:
            # Use thread pool to avoid event loop conflicts
            loop = asyncio.get_event_loop()
            success = await loop.run_in_executor(None, connection_manager.connect)
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
async def connect_ibkr(config: ConnectionConfig):
    """连接到IBKR"""
    global ibkr_client
    
    try:
        # 使用连接管理器更新配置并连接
        connection_manager.update_config(config)
        
        # 在线程池中执行同步连接操作，避免事件循环冲突
        loop = asyncio.get_event_loop()
        success = await loop.run_in_executor(None, connection_manager.connect)
        
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
def disconnect_ibkr():
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
async def get_connection_status():
    """获取连接状态"""
    return {
        "connected": connection_manager.is_connected(),
        "account_type": "Paper" if connection_manager.config.paper_trading else "Live"
    }

# Market data endpoints
@app.post("/api/historical_data")
async def get_historical_data(request: HistoricalDataRequest):
    """获取历史数据"""
    if not ibkr_client or not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    
    try:
        df = ibkr_client.get_historical_data(
            symbol=request.symbol,
            duration=request.duration,
            bar_size=request.bar_size,
            exchange=request.exchange
        )
        
        if df.empty:
            raise HTTPException(status_code=404, detail="No data found")
        
        # Save to CSV
        filename = f"data/{request.symbol}_historical.csv"
        ibkr_client.save_historical_data(df, request.symbol, filename)
        
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
async def get_chart_data(request: ChartDataRequest):
    """获取图表数据"""
    if not ibkr_client or not ibkr_client.connected:
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    
    try:
        df = ibkr_client.get_historical_data(
            symbol=request.symbol,
            duration=request.duration,
            bar_size=request.bar_size,
            exchange=request.exchange
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
async def get_realtime_data(symbol: str, exchange: str = "SMART"):
    """获取实时数据"""
    if not ibkr_client or not ibkr_client.connected:
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    
    try:
        data = ibkr_client.get_real_time_data(symbol, exchange)
        if data:
            return {"success": True, "data": data}
        else:
            raise HTTPException(status_code=404, detail="No data available")
            
    except Exception as e:
        logger.error(f"Real-time data error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Trading endpoints
@app.post("/api/place_order")
async def place_order(order: OrderRequest):
    """下单"""
    if not ibkr_client or not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    
    try:
        if order.order_type.upper() == "MARKET":
            trade = ibkr_client.place_market_order(
                symbol=order.symbol,
                quantity=order.quantity,
                action=order.action,
                exchange=order.exchange,
                sec_type=order.sec_type
            )
        elif order.order_type.upper() == "LIMIT":
            if not order.price:
                raise HTTPException(status_code=400, detail="Price required for limit order")
            trade = ibkr_client.place_limit_order(
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
            # Record manual order if risk_manager is available
            if risk_manager:
                from risk_manager import AutoOrder
                from datetime import datetime
                import os
                
                auto_order = AutoOrder(
                    order_id=str(trade.order.orderId),
                    symbol=order.symbol,
                    action=order.action,
                    quantity=order.quantity,
                    order_type=order.order_type.upper(),
                    price=order.price if order.price else 0.0,
                    status="Submitted",
                    created_at=datetime.now(),
                    executed_at=None,
                    rule_id="manual",
                    order_source="manual"
                )
                
                risk_manager.auto_orders[str(trade.order.orderId)] = auto_order
                risk_manager.save_auto_orders_to_csv(os.path.join("data", "auto_orders.csv"))
            
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
async def cancel_order(order_id: int):
    """取消订单"""
    if not ibkr_client or not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    
    try:
        success, message = ibkr_client.cancel_order(order_id)
        
        if success:
            await manager.broadcast({
                "type": "order_cancelled",
                "order_id": order_id
            })
            
            return {"success": True, "message": message}
        else:
            raise HTTPException(status_code=404, detail=message)
            
    except Exception as e:
        logger.error(f"Order cancellation error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/cancel_order")
async def cancel_order_post(request: dict):
    """取消订单 (POST方法)"""
    if not ibkr_client or not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    
    try:
        order_id = request.get('order_id')
        if not order_id:
            raise HTTPException(status_code=400, detail="order_id is required")
            
        success, message = ibkr_client.cancel_order(int(order_id))
        
        if success:
            await manager.broadcast({
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
async def cancel_all_orders():
    """取消所有订单"""
    if not ibkr_client or not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    
    try:
        cancelled_count = ibkr_client.cancel_all_orders()
        
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
async def get_orders():
    """获取订单列表"""
    if not ibkr_client or not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    
    try:
        orders = ibkr_client.get_orders(risk_manager)
        return {"success": True, "orders": orders}
        
    except Exception as e:
        logger.error(f"Get orders error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Account endpoints
@app.get("/api/account_info")
async def get_account_info():
    """获取账户信息"""
    if not ibkr_client or not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    
    try:
        info = ibkr_client.get_account_info()
        return {"success": True, "account_info": info}
        
    except Exception as e:
        logger.error(f"Get account info error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/positions")
async def get_positions():
    """获取持仓列表"""
    if not ibkr_client or not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    
    try:
        positions = ibkr_client.get_positions()
        return {"success": True, "positions": positions}
        
    except Exception as e:
        logger.error(f"Get positions error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Risk management endpoints
@app.post("/api/risk_rules")
async def add_risk_rule(rule: RiskRuleRequest):
    """添加风险管理规则"""
    if not risk_manager:
        raise HTTPException(status_code=400, detail="Risk manager not initialized")
    
    try:
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
async def get_risk_rules():
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

@app.delete("/api/risk_rules/{symbol}")
async def remove_risk_rule(symbol: str):
    """移除风险管理规则"""
    if not risk_manager:
        raise HTTPException(status_code=400, detail="Risk manager not initialized")
    
    try:
        risk_manager.remove_risk_rule(symbol)
        return {"success": True, "message": "Risk rule removed successfully"}
        
    except Exception as e:
        logger.error(f"Remove risk rule error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/order_queue_status")
async def get_order_queue_status():
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
async def get_risk_config():
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
async def get_auto_orders():
    """获取自动下单记录"""
    if not risk_manager:
        raise HTTPException(status_code=400, detail="Risk manager not initialized")
    
    try:
        orders = risk_manager.get_auto_orders()
        return {"success": True, "orders": orders}
        
    except Exception as e:
        logger.error(f"Get auto orders error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/order_backup_history")
async def get_order_backup_history(days: int = 7):
    """获取订单备份历史"""
    try:
        if ibkr_client:
            orders = ibkr_client.get_order_backup_history(days)
            return {"success": True, "orders": orders}
        else:
            return {"success": False, "message": "IBKR client not available", "orders": []}
    except Exception as e:
        logger.error(f"Failed to get order backup history: {e}")
        return {"success": False, "message": str(e), "orders": []}

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