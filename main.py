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
from zoneinfo import ZoneInfo
from typing import List, Optional, Dict, Any
from loguru import logger
from dotenv import load_dotenv
from dataclasses import asdict
import uuid
import contextvars
import time
import re
from contextlib import asynccontextmanager

# FastAPI imports
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Depends, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel
from starlette.middleware.gzip import GZipMiddleware

# Add current directory to Python path
sys.path.append(str(Path(__file__).parent))

import core
from src.connection_manager import connection_manager, ConnectionConfig
from src.database import get_db_manager
from src.models import (
    HistoricalDataRequest,
    ChartDataRequest,
    OrderRequest,
    ExternalStrategyOrderRequest,
    StrategySettingsRequest,
    StrategyRequest,
    RiskRuleRequest,
    RiskConfigUpdateRequest,
    PositionSizingRequest,
    CurrentPriceRequest,
)

# Shared application services
from core import (
    ibkr_client,
    telegram_notifier,
    notification_manager,
    risk_manager,
    strategy_manager,
    queue_manager,
    websocket_manager,
    order_manager,
)

# Authentication and routers
from src.routes.auth import get_current_user, router as auth_router
from src.routes.connection import router as connection_router
from src.routes.market_data import router as market_data_router
from src.routes.account import router as account_router
from src.routes.orders import router as orders_router
from src.routes.risk import router as risk_router
from src.routes.strategy import router as strategy_router
from src.routes.notifications import router as notifications_router
from src.routes.queue import router as queue_router
from src.routes.logs import router as logs_router
from src.routes.websocket import router as websocket_router
from src.routes.dom import router as dom_router


# Global variables
load_dotenv()

# 优化：可配置的PnL广播间隔（秒），默认5秒
PNL_BROADCAST_INTERVAL_SEC = int(os.getenv('PNL_BROADCAST_INTERVAL_SEC', '5'))

# Context variables for request-scoped logging
request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
module_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("module", default="-")


# Timezone helpers
LOCAL_TZ = datetime.now().astimezone().tzinfo
NY_TZ = ZoneInfo("America/New_York")



def resolve_module(path: str) -> str:
    """Simple mapping from request path to module name."""
    if path.startswith("/api/strategies/internal"):
        return "internal_strategy"
    if path.startswith("/api/strategies"):
        return "external_strategy"
    if "/risk" in path:
        return "risk_rule"
    if "/orders" in path:
        return "orders"
    if any(p in path for p in ["/portfolio", "/positions"]):
        return "portfolio"
    if any(p in path for p in ["/connect", "/disconnect", "/connection"]):
        return "connection"
    return "general"

# 临时创建FastAPI应用实例（稍后会用lifespan重新创建）
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    auto_connect = os.getenv('AUTO_CONNECT', 'False').lower() == 'true'
    main_logger = get_module_logger("main")
    
    if auto_connect:
        main_logger.info("Auto-connect enabled, will attempt to connect to IBKR asynchronously...")
        
        async def async_auto_connect():
            try:
                # Run connection in executor to avoid blocking
                loop = asyncio.get_event_loop()
                success = await loop.run_in_executor(None, connection_manager.connect)
                if not success:
                    main_logger.warning("IBKR auto-connection failed. Continuing without connection...")
                    main_logger.info("You can connect later through the web interface.")
                else:
                    main_logger.info("IBKR auto-connection successful!")
                    if core.ibkr_client is None:
                        core.ibkr_client = core.IBKRClient()
                        main_logger.info("IBKRClient initialized after auto-connect")

                    ibkr_client_local = core.ibkr_client

                    # Update risk_manager with ibkr_client
                    try:
                        risk_manager.ibkr_client = ibkr_client_local
                        main_logger.info("Risk manager ibkr_client updated with auto-connected client")
                    except Exception as re_err:
                        main_logger.warning(f"RiskManager ibkr_client update warning: {re_err}")

                    # Update queue_manager dependencies
                    try:
                        queue_manager.set_dependencies(
                            risk_manager=risk_manager,
                            strategy_manager=strategy_manager,
                            connection_manager=connection_manager,
                            websocket_manager=websocket_manager,
                            order_manager=order_manager,
                        )
                        main_logger.info("Queue manager dependencies updated after auto-connect")
                    except Exception as dep_err:
                        main_logger.warning(f"QueueManager set_dependencies warning: {dep_err}")
            except Exception as e:
                main_logger.warning(f"Auto-connection failed: {e}")
                main_logger.info("You can connect later through the web interface.")
        
        # Schedule async connection to run in background
        asyncio.create_task(async_auto_connect())
    else:
        main_logger.info("Auto-connect disabled. You can connect later through the web interface.")
    
    # START risk manager scheduler within the running event loop
    try:
        risk_manager.start()
        main_logger.info("Risk manager started during application startup")
    except Exception as e:
        main_logger.warning(f"Risk manager startup warning: {e}")
    
    yield
    # Shutdown
    main_logger.info("Application shutting down...")
    # Gracefully shutdown risk manager scheduler
    try:
        if getattr(risk_manager, 'scheduler', None) and risk_manager.scheduler.running:
            risk_manager.scheduler.shutdown()
            main_logger.info("Risk manager scheduler shutdown")
    except Exception as e:
        main_logger.warning(f"Risk manager shutdown warning: {e}")

# 重新创建带有lifespan的FastAPI应用
app = FastAPI(title="IBKR Trading Bot", version="1.0.0", lifespan=lifespan)

@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Attach a request_id to each request and log basic metrics."""
    request_id = str(uuid.uuid4())
    path = request.url.path
    module = resolve_module(path)
    token_id = request_id_ctx.set(request_id)
    token_mod = module_ctx.set(module)
    start = time.time()
    try:
        response = await call_next(request)
        duration = (time.time() - start) * 1000
        logger.info(
            f"REQUEST {request.method} {path} status={response.status_code} duration={duration:.2f}ms"
        )
    finally:
        request_id_ctx.reset(token_id)
        module_ctx.reset(token_mod)
    response.headers["X-Request-ID"] = request_id
    return response


# 静态资源缓存中间件：为静态资源添加缓存头
@app.middleware("http")
async def static_cache_middleware(request: Request, call_next):
    response = await call_next(request)
    try:
        path = request.url.path
        if request.method == "GET" and path.startswith("/static/") and response.status_code == 200:
            if any(path.endswith(ext) for ext in (".js", ".css", ".svg", ".woff2", ".ttf")):
                response.headers["Cache-Control"] = "public, max-age=604800, immutable"
    except Exception:
        pass
    return response

    # 默认按分钟处理
    return int(n)

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

# RapiDoc for main service
@app.get("/api-docs.json", include_in_schema=False)
async def get_api_docs_main():
    return JSONResponse(app.openapi())

@app.get("/rapidoc", response_class=HTMLResponse, include_in_schema=False)
async def rapidoc_html_main():
    return HTMLResponse(
        """
        <!doctype html>
        <html>
          <head>
            <meta charset="utf-8" />
            <meta name="viewport" content="width=device-width, initial-scale=1" />
            <title>IBKR Trading Bot API - RapiDoc</title>
            <script type="module" src="https://unpkg.com/rapidoc/dist/rapidoc-min.js"></script>
            <style>
              body { margin: 0; padding: 0; }
              #paste-last { position: fixed; right: 16px; top: 12px; z-index: 9999; }
            </style>
            <script>
              const STORAGE_KEY = 'rapidoc:lastRequestBody';
              window.addEventListener('DOMContentLoaded', () => {
                const rd = document.querySelector('rapi-doc');
                if (!rd) return;
                rd.addEventListener('before-try', (e) => {
                  try {
                    const body = e?.detail?.requestBody;
                    if (body) localStorage.setItem(STORAGE_KEY, JSON.stringify(body, null, 2));
                  } catch (_) {}
                });
              });
            </script>
          </head>
          <body>
            <button id="paste-last">复制上次请求体</button>
            <script>
              document.getElementById('paste-last').addEventListener('click', () => {
                const cache = localStorage.getItem('rapidoc:lastRequestBody');
                if (cache) {
                  navigator.clipboard.writeText(cache).then(()=>alert('已复制到剪贴板')).catch(()=>alert('复制失败，请手动复制'));
                } else {
                  alert('暂无缓存的请求体');
                }
              });
            </script>
            <rapi-doc
              spec-url="/api-docs.json"
              render-style="read"
              show-header="true"
              allow-try="true"
              theme="light"
              primary-color="#0d6efd"
              nav-bg-color="#f8f9fa"
              regular-font="Inter, system-ui, -apple-system, Segoe UI, Roboto, Ubuntu, Cantarell, Noto Sans, Helvetica Neue, Arial, Apple Color Emoji, Segoe UI Emoji">
            </rapi-doc>
          </body>
        </html>
        """,
        status_code=200,
    )

# 自定义 OpenAPI：仅保留主服务分组

def _ensure_tag(tags_list, name: str, description: str):
    if not any(t.get("name") == name for t in tags_list):
        tags_list.append({"name": name, "description": description})


def custom_openapi():
    if getattr(app, "openapi_schema", None):
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description="IBKR Trading Bot API",
        routes=app.routes,
    )

    # 顶层服务器（主服务）
    main_host = os.getenv("WEB_HOST", "127.0.0.1")
    main_port = os.getenv("WEB_PORT", "8000")
    schema["servers"] = [{"url": f"http://{main_host}:{main_port}"}]

    # 确保 OpenAPI 中包含安全方案，以便 RapiDoc 提供 Authentication 设置
    components = schema.setdefault("components", {})
    security_schemes = components.setdefault("securitySchemes", {})
    if "bearerAuth" not in security_schemes:
        security_schemes["bearerAuth"] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "JWT 令牌，形如：Bearer eyJhbGciOi..."
        }
    if "strategyToken" not in security_schemes:
        security_schemes["strategyToken"] = {
            "type": "apiKey",
            "in": "header",
            "name": "X-Strategy-Token",
            "description": "策略侧可选令牌，可与 Authorization 并存"
        }

    # Tag 分组
    schema.setdefault("tags", [])
    _ensure_tag(schema["tags"], "主服务", "主服务 API")

    # 设置全局默认安全要求（如需取消，可在具体 operation 设置 security: []）
    if not schema.get("security"):
        schema["security"] = [{"bearerAuth": []}]

    # 为所有 operation 添加“主服务”标签
    for path, methods in list(schema.get("paths", {}).items()):
        for method, op in list(methods.items()):
            if method.lower() not in {"get", "post", "put", "delete", "patch", "options", "head"}:
                continue
            op.setdefault("tags", [])
            if "主服务" not in op["tags"]:
                op["tags"].append("主服务")

    app.openapi_schema = schema
    return app.openapi_schema

# 覆盖默认 openapi 生成器
app.openapi = custom_openapi

# 启用GZip压缩，减小大文件(如 app.js)传输体积
app.add_middleware(GZipMiddleware, minimum_size=1024)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:3000",
        "http://127.0.0.1:3000"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
app.add_api_route("/", index_page)
app.add_api_route("/api-docs.json", get_api_docs_main, include_in_schema=False)
app.add_api_route("/rapidoc", rapidoc_html_main, include_in_schema=False)

# Register routers
app.include_router(auth_router)
app.include_router(connection_router)
app.include_router(market_data_router)
app.include_router(account_router)
app.include_router(orders_router)
app.include_router(risk_router)
app.include_router(strategy_router)
app.include_router(notifications_router)
app.include_router(queue_router)
app.include_router(logs_router)
app.include_router(websocket_router)
app.include_router(dom_router)

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


def get_module_logger(module_name: str):
    """Get a logger with a specific module name."""
    return logger.bind(module=module_name, request_id="-")


def setup_logging():
    """Configure loguru to log to console and file.
    确保 logs/trading_bot.log 存在，并设置合理的滚动与保留策略。
    """
    global logger
    try:
        os.makedirs("logs", exist_ok=True)
    except Exception:
        # 即便创建失败也不崩溃
        pass

    # 重置默认 handler，添加控制台和文件输出
    try:
        logger.remove()
    except Exception:
        pass

    # 设置默认 extra，确保格式化时不会缺失
    logger.configure(extra={"request_id": "-", "module": "-"})

    def _inject_ctx(record):
        extra = record["extra"]
        rid = request_id_ctx.get()
        mod = module_ctx.get()
        if rid and (not extra.get("request_id") or extra.get("request_id") == "-"):
            extra["request_id"] = rid
        if mod and (not extra.get("module") or extra.get("module") == "-"):
            extra["module"] = mod
    logger = logger.patch(_inject_ctx)

    # 统一格式，便于 /api/logs 按等级过滤：会匹配 "| LEVEL |"
    log_format = (
        "| {time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {extra[module]:<15} | "
        "{extra[request_id]:<36} | {name}:{function}:{line} - {message}"
    )

    # 控制台
    logger.add(sys.stderr, level="DEBUG", format=log_format, enqueue=True, backtrace=False, diagnose=False)

    # 文件
    logger.add(
        "logs/trading_bot.log",
        rotation="10 MB",
        retention="14 days",
        encoding="utf-8",
        enqueue=True,
        level="DEBUG",
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

# 添加：全局任务引用用于定期广播账户与持仓（实时PnL展示）
pnl_broadcast_task = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    main_logger = get_module_logger("main")
    
    # 启动时的初始化
    try:
        # 在事件循环中启动 risk_manager
        risk_manager.start()
        main_logger.info("Risk manager started during application startup")
    except Exception as e:
        main_logger.warning(f"Risk manager startup warning: {e}")

    try:
        strategy_manager.start()
        main_logger.info("Strategy manager started during application startup")
    except Exception as e:
        main_logger.warning(f"Strategy manager startup warning: {e}")

    # 启动定期通过WebSocket广播账户与持仓信息（用于实时PnL展示）
    # 现在使用异步调用，不会阻塞主线程
    try:
        global pnl_broadcast_task
        if pnl_broadcast_task is None:
            pnl_broadcast_task = asyncio.create_task(_pnl_broadcast_loop())
            main_logger.info("Started PnL broadcast loop task with async calls")
    except Exception as e:
        main_logger.warning(f"Failed to start PnL broadcast loop: {e}")
    
    yield
    
    # 关闭时的清理
    try:
        if hasattr(risk_manager, 'scheduler') and risk_manager.scheduler.running:
            risk_manager.scheduler.shutdown()
            main_logger.info("Risk manager scheduler shutdown")
    except Exception as e:
        main_logger.warning(f"Risk manager shutdown warning: {e}")

# 添加：定期通过WebSocket广播账户与持仓信息（用于实时PnL展示）
async def _pnl_broadcast_loop():
    try:
        while True:
            await asyncio.sleep(PNL_BROADCAST_INTERVAL_SEC)
            # 若没有活跃的WebSocket连接，跳过数据拉取与广播，降低负载
            if not websocket_manager.connections:
                continue
            try:
                    # 仅在已连接且 ibkr_client 可用时运行
                    if not connection_manager.is_connected() or 'ibkr_client' not in globals() or ibkr_client is None:
                        continue
                    account_info = None
                    positions = None
                    try:
                        account_info = await asyncio.wait_for(
                            ibkr_client.get_account_info(),
                            timeout=5.0
                        )
                    except Exception:
                        account_info = None
                    try:
                        positions = await asyncio.wait_for(
                            ibkr_client.get_positions(fast=True),
                            timeout=5.0
                        )
                    except Exception:
                        positions = None
                    if account_info is not None:
                        await websocket_manager.broadcast({
                            "type": "account_update",
                            "account_info": account_info
                        })
                    if positions is not None:
                        await websocket_manager.broadcast({
                            "type": "position_update",
                            "positions": positions
                        })
            except Exception as be:
                logger.debug(f"PnL broadcast iteration error: {be}")
    except asyncio.CancelledError:
        # 任务被取消时正常退出
        pass
    except Exception as e:
        logger.warning(f"PnL broadcast loop error: {e}")

# 实时行情推送任务
# Strategy APIs

# Queue Management APIs

# Strategy Executions API
# Notifications API
# System Logs API (kept for general debugging)


def main():
    """Main application entry point"""
    print_banner()
    
    # Setup
    setup_logging()
    create_directories()
    
    main_logger = get_module_logger("main")
    main_logger.info("Starting IBKR Trading Bot...")
    
    # Check environment
    if not check_environment():
        main_logger.error("Environment check failed. Please configure .env file.")
        return
    
    # Auto-connect is now handled in FastAPI lifespan event
    
    # Start web server
    main_logger.info("Starting web server...")
    web_host = os.getenv('WEB_HOST', '0.0.0.0')
    web_port = int(os.getenv('WEB_PORT', '8000'))
    display_host = 'localhost' if web_host in ('0.0.0.0', '::') else web_host
    main_logger.info(f"Web UI available at: http://{display_host}:{web_port}")
    main_logger.info("Press Ctrl+C to stop the application")
    
    try:
        import uvicorn
        uvicorn.run(
            app,
            host=os.getenv('WEB_HOST', '0.0.0.0'),
            port=int(os.getenv('WEB_PORT', '8000')),
            log_level="warning"  # 减少HTTP请求日志显示
        )
        
    except KeyboardInterrupt:
        main_logger.info("Shutting down...")
    except Exception as e:
        main_logger.error(f"Application error: {e}")
    finally:
        main_logger.info("Application stopped")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nApplication interrupted by user")
    except Exception as e:
        print(f"Fatal error: {e}")
        sys.exit(1)
