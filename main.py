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
import uuid
import contextvars
import time
import re

# FastAPI imports
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Depends, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel
from jose import JWTError, jwt
import httpx

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

# Mock server configuration
MOCK_HOST = os.getenv('MOCK_HOST', '127.0.0.1')
MOCK_PORT = int(os.getenv('MOCK_PORT', '18080'))
MOCK_BASE = f"http://{MOCK_HOST}:{MOCK_PORT}/api"
# 优化：可配置的PnL广播间隔（秒），默认5秒
PNL_BROADCAST_INTERVAL_SEC = int(os.getenv('PNL_BROADCAST_INTERVAL_SEC', '5'))

# Context variables for request-scoped logging
request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
module_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("module", default="-")


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

def is_mock_mode() -> bool:
    try:
        # 优先使用运行时的连接配置端口来判断是否为Mock模式
        port = int(getattr(connection_manager.config, 'port', MOCK_PORT))
    except Exception:
        port = MOCK_PORT
    return port == MOCK_PORT

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

# 自定义 OpenAPI：为 /api/mock/* 设置独立 servers，并按“主服务/Mock 网关”分组

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
    _ensure_tag(schema["tags"], "Mock 网关", "Mock 网关 API")

    # 设置全局默认安全要求（如需取消，可在具体 operation 设置 security: []）
    if not schema.get("security"):
        schema["security"] = [{"bearerAuth": []}]

    # 为 /api/mock/* 设置 per-operation servers，并分配 Tag
    # 特殊处理：trigger_strategy 属于主服务，不应指向 mock 地址
    for path, methods in list(schema.get("paths", {}).items()):
        is_mock = path.startswith("/api/mock/") and path != "/api/mock/trigger_strategy"
        for method, op in list(methods.items()):
            if method.lower() not in {"get", "post", "put", "delete", "patch", "options", "head"}:
                continue
            op.setdefault("tags", [])
            if is_mock:
                if "Mock 网关" not in op["tags"]:
                    op["tags"].append("Mock 网关")
                op["servers"] = [{"url": f"http://{MOCK_HOST}:{MOCK_PORT}"}]
                # Mock 接口默认不要求认证
                op["security"] = []
                # 注入更有意义的示例
                if method.lower() == "post" and path in {"/api/mock/set_price", "/api/mock/feed_data", "/api/mock/reset"}:
                    op.setdefault("requestBody", {}).setdefault("content", {}).setdefault("application/json", {}).setdefault("examples", {})
                    examples = op["requestBody"]["content"]["application/json"]["examples"]
                    if path == "/api/mock/set_price":
                        examples.update({
                            "mnq_set_price": {
                                "summary": "设置 MNQ 价格",
                                "value": {"symbol": "MNQ", "price": 18000.5}
                            }
                        })
                    elif path == "/api/mock/feed_data":
                        examples.update({
                            "mnq_feed_1d": {
                                "summary": "推送 MNQ 1D/5mins 数据，200ms 间隔",
                                "value": {"symbol": "MNQ", "duration": "1 D", "bar_size": "5 mins", "delay_ms": 200}
                            }
                        })
            else:
                if "主服务" not in op["tags"]:
                    op["tags"].append("主服务")
                # 为 trigger_strategy 注入示例
                if path == "/api/mock/trigger_strategy" and method.lower() == "post":
                    op.setdefault("requestBody", {}).setdefault("content", {}).setdefault("application/json", {}).setdefault("examples", {})
                    op["requestBody"]["content"]["application/json"]["examples"].update({
                        "market_buy_mnq": {
                            "summary": "Buy down and Sell open 在 MNQ 上市价买 1 手",
                            "value": {
                                "strategy": "Buy down and Sell open",
                                "symbol": "MNQ",
                                "action": "BUY",
                                "qty": 1,
                                "type": "MARKET",
                                "price": None,
                                "set_price": False,
                                "exchange": "SMART",
                                "sec_type": "FUT"
                            }
                        },
                        "limit_buy_mnq": {
                            "summary": "Buy the Dip Strategy 在 MNQ 上挂 17950 的限价买 1 手（先设置价格）",
                            "value": {
                                "strategy": "Buy the Dip Strategy",
                                "symbol": "MNQ",
                                "action": "BUY",
                                "qty": 1,
                                "type": "LIMIT",
                                "price": 17950,
                                "set_price": True,
                                "exchange": "SMART",
                                "sec_type": "FUT"
                            }
                        }
                    })

    app.openapi_schema = schema
    return app.openapi_schema

# 覆盖默认 openapi 生成器
app.openapi = custom_openapi

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

    logger = logger.patch(
        lambda record: record["extra"].update(
            request_id=request_id_ctx.get(), module=module_ctx.get()
        )
    )

    # 统一格式，便于 /api/logs 按等级过滤：会匹配 "| LEVEL |"
    log_format = (
        "| {time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {extra[module]:<15} | "
        "{extra[request_id]:<36} | {name}:{function}:{line} - {message}"
    )

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
        print(req.username, req.password)
        print(ADMIN_USERNAME, ADMIN_PASSWORD)
        print(req.username != ADMIN_USERNAME)
        print(req.password != ADMIN_PASSWORD)
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
# 注入依赖：让 StrategyManager 能访问 RiskManager（用于自动创建风控规则）
try:
    strategy_manager.set_dependencies(risk_manager=risk_manager)
except Exception as _sm_dep_err:
    logger.warning(f"StrategyManager set_dependencies at startup failed: {_sm_dep_err}")
websocket_connections = []
order_manager = OrderManager()
# 注入 OrderManager 到 RiskManager（用于软删除等操作）
try:
    risk_manager.set_dependencies(order_manager=order_manager)
except Exception as _rm_dep_err:
    logger.warning(f"RiskManager set_dependencies at startup failed: {_rm_dep_err}")

# 添加：全局任务引用用于定期广播账户与持仓（实时PnL展示）
pnl_broadcast_task = None

# 添加：定期通过WebSocket广播账户与持仓信息（用于实时PnL展示）
async def _pnl_broadcast_loop():
    try:
        while True:
            await asyncio.sleep(PNL_BROADCAST_INTERVAL_SEC)
            # 若没有活跃的WebSocket连接，跳过数据拉取与广播，降低负载
            if not websocket_manager.connections:
                continue
            try:
                if is_mock_mode():
                    # 从模拟服务器获取数据并广播
                    async with httpx.AsyncClient() as client:
                        account_info = None
                        positions = None
                        try:
                            resp_ai = await client.get(f"{MOCK_BASE}/account_info")
                            if resp_ai.status_code == 200:
                                data_ai = resp_ai.json()
                                account_info = data_ai.get("account_info") or data_ai
                        except Exception:
                            account_info = None
                        try:
                            resp_pos = await client.get(f"{MOCK_BASE}/positions")
                            if resp_pos.status_code == 200:
                                data_pos = resp_pos.json()
                                positions = data_pos.get("positions") or data_pos
                        except Exception:
                            positions = None
                        if account_info is not None:
                            await websocket_manager.broadcast({
                                "type": "account_update",
                                "account_info": account_info
                            })
                        if positions is not None:
                            await websocket_manager.broadcast({
                                "type": "positions_update",
                                "positions": positions
                            })
                else:
                    # 仅在已连接且 ibkr_client 可用时运行
                    if not connection_manager.is_connected() or 'ibkr_client' not in globals() or ibkr_client is None:
                        continue
                    loop = asyncio.get_event_loop()
                    account_info = None
                    positions = None
                    try:
                        account_info = await asyncio.wait_for(
                            loop.run_in_executor(None, ibkr_client.get_account_info),
                            timeout=5.0
                        )
                    except Exception:
                        account_info = None
                    try:
                        positions = await asyncio.wait_for(
                            loop.run_in_executor(None, lambda: ibkr_client.get_positions(fast=True)),
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
                            "type": "positions_update",
                            "positions": positions
                        })
            except Exception as be:
                logger.debug(f"PnL broadcast iteration error: {be}")
    except asyncio.CancelledError:
        # 任务被取消时正常退出
        pass
    except Exception as e:
        logger.warning(f"PnL broadcast loop error: {e}")

# FastAPI 启动和关闭事件
@app.on_event("startup")
async def startup_event():
    """应用启动时的初始化"""
    try:
        # 在事件循环中启动 risk_manager
        risk_manager.start()
        logger.info("Risk manager started during application startup")
    except Exception as e:
        logger.warning(f"Risk manager startup warning: {e}")

    # 启动定期通过WebSocket广播账户与持仓信息（用于实时PnL展示）
    try:
        global pnl_broadcast_task
        if pnl_broadcast_task is None:
            pnl_broadcast_task = asyncio.create_task(_pnl_broadcast_loop())
            logger.info("Started PnL broadcast loop task")
    except Exception as e:
        logger.warning(f"Failed to start PnL broadcast loop: {e}")

@app.on_event("shutdown")
async def shutdown_event():
    """应用关闭时的清理"""
    try:
        if hasattr(risk_manager, 'scheduler') and risk_manager.scheduler.running:
            risk_manager.scheduler.shutdown()
            logger.info("Risk manager scheduler shutdown")
    except Exception as e:
        logger.warning(f"Risk manager shutdown warning: {e}")

    # 关闭PnL广播任务
    try:
        global pnl_broadcast_task
        if pnl_broadcast_task is not None and not pnl_broadcast_task.done():
            pnl_broadcast_task.cancel()
            pnl_broadcast_task = None
            logger.info("Stopped PnL broadcast loop task")
    except Exception as e:
        logger.warning(f"Failed to stop PnL broadcast loop: {e}")

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

@app.get("/api/connection_config")
async def get_connection_config():
    """获取默认连接配置（不需要认证）"""
    try:
        default_config = connection_manager.get_config()
        return {
            "host": default_config.host,
            "port": default_config.port,
            "client_id": default_config.client_id,
            "paper_trading": default_config.paper_trading
        }
    except Exception as e:
        logger.error(f"Failed to get connection config: {e}")
        raise HTTPException(status_code=500, detail="Failed to get connection config")

@app.post("/api/connect")
async def connect_ibkr(config: ConnectionConfig, user: Optional[str] = Depends(get_current_user)):
    """连接到IBKR或Mock (当端口为18080时)"""
    global ibkr_client
    
    try:
        # 更新配置
        connection_manager.update_config(config)
        
        # 若为mock模式，则调用mock连接端点并标记状态
        if config.port == MOCK_PORT or is_mock_mode():
            logger.info(f"Entering MOCK mode on {MOCK_BASE}")
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.post(f"{MOCK_BASE}/connect")
                    if resp.status_code != 200:
                        raise HTTPException(status_code=resp.status_code, detail=resp.text)
                    data = resp.json()
                    if not data.get("success"):
                        raise HTTPException(status_code=500, detail="Mock server connect failed")
            except httpx.RequestError as e:
                logger.error(f"Mock server connect failed: {e}")
                raise HTTPException(status_code=503, detail="Mock server unavailable")

            # 标记连接状态为True，仅用于UI
            connection_manager.connected = True
            # 确保队列worker已启动
            await queue_manager.start_worker()
            return {"success": True, "message": "Connected to MOCK server"}
        
        # 正常IBKR连接流程
        loop = asyncio.get_event_loop()
        try:
            # 添加超时机制避免无限期阻塞
            await asyncio.wait_for(
                loop.run_in_executor(None, lambda: connection_manager.disconnect()),
                timeout=15.0
            )
            success = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: connection_manager.connect()),
                timeout=30.0
            )
        except asyncio.TimeoutError:
            logger.error("Connection operation timed out")
            raise HTTPException(status_code=408, detail="Connection operation timed out")
        
        if success:
            # 建立/更新 ibkr_client 引用
            if ibkr_client is None:
                ibkr_client_local = IBKRClient()
            else:
                ibkr_client_local = ibkr_client
            
            # 更新 risk_manager 的 ibkr_client（RiskManager 依赖 ibkr_client）
            try:
                risk_manager.ibkr_client = ibkr_client_local
                # 确保 RiskManager 已注入 OrderManager 引用
                try:
                    risk_manager.set_dependencies(order_manager=order_manager)
                except Exception as _rm_dep_err:
                    logger.warning(f"RiskManager set_dependencies after connect failed: {_rm_dep_err}")
                logger.info("Risk manager ibkr_client updated after connection")
            except Exception as re_err:
                logger.warning(f"RiskManager ibkr_client update warning: {re_err}")
            
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
        
        # 若在mock模式，调用mock断开端点
        if is_mock_mode():
            try:
                async with httpx.AsyncClient() as client:
                    await client.post(f"{MOCK_BASE}/disconnect")
            except httpx.RequestError as e:
                logger.warning(f"Mock server disconnect call failed: {e}")
            # 无论mock断开是否成功，都重置本地连接状态
            connection_manager.connected = False
            return {"success": True, "message": "Disconnected from MOCK server"}

        connection_manager.disconnect()
        
        return {"success": True, "message": "Disconnected successfully"}
        
    except Exception as e:
        logger.error(f"Disconnection error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/connection_status")
async def get_connection_status(user: Optional[str] = Depends(get_current_user)):
    """获取连接状态"""
    if is_mock_mode():
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(f"{MOCK_BASE}/connection_status")
                if response.status_code == 200:
                    mock_data = response.json()
                    return {
                        "connected": mock_data.get("connected", False),
                        "account_type": "Mock"
                    }
                else:
                    return {"connected": False, "account_type": "Mock"}
        except Exception as e:
            logger.warning(f"Mock server connection failed: {e}")
            return {"connected": False, "account_type": "Mock"}
    
    return {
        "connected": connection_manager.is_connected(),
        "account_type": "Paper" if connection_manager.config.paper_trading else "Live"
    }

# Market data endpoints
@app.post("/api/historical_data")
async def get_historical_data(request: HistoricalDataRequest, user: Optional[str] = Depends(get_current_user)):
    """获取历史数据"""
    if is_mock_mode():
        try:
            # Convert request to dict for mock server
            request_data = {
                "symbol": request.symbol,
                "duration": request.duration,
                "bar_size": request.bar_size,
                "exchange": request.exchange
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.post(f"{MOCK_BASE}/historical_data", json=request_data)
                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 404:
                    raise HTTPException(status_code=404, detail="No historical data found")
                else:
                    raise HTTPException(status_code=response.status_code, detail=response.text)
                    
        except httpx.RequestError as e:
            logger.error(f"Mock server request failed: {e}")
            raise HTTPException(status_code=503, detail="Mock server unavailable")
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Mock historical data error: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    if not ibkr_client or not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    
    try:
        # Run in executor to avoid blocking event loop
        loop = asyncio.get_event_loop()
        try:
            df = await asyncio.wait_for(
                loop.run_in_executor(
                    None, 
                    lambda: ibkr_client.get_historical_data(
                        symbol=request.symbol,
                        duration=request.duration,
                        bar_size=request.bar_size,
                        exchange=request.exchange
                    )
                ),
                timeout=30.0  # 30-second timeout for historical data
            )
        except asyncio.TimeoutError:
            logger.error(f"Historical data request timed out for {request.symbol}")
            raise HTTPException(status_code=408, detail="Historical data request timed out")
        
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
    if is_mock_mode():
        try:
            request_data = {
                "symbol": request.symbol,
                "duration": request.duration,
                "bar_size": request.bar_size,
                "exchange": request.exchange
            }
            # 透传 data_type 给 mock（若 mock 忽略也不影响）
            if hasattr(request, 'data_type') and request.data_type:
                request_data["data_type"] = request.data_type
            async with httpx.AsyncClient() as client:
                response = await client.post(f"{MOCK_BASE}/chart_data", json=request_data)
                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 404:
                    raise HTTPException(status_code=404, detail="No chart data found")
                else:
                    raise HTTPException(status_code=response.status_code, detail=response.text)
        except httpx.RequestError as e:
            logger.error(f"Mock server request failed: {e}")
            raise HTTPException(status_code=503, detail="Mock server unavailable")
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Mock chart data error: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    if not ibkr_client or not ibkr_client.connected:
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    
    try:
        # Run in executor to avoid blocking event loop
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        try:
            df = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: ibkr_client.get_historical_data(
                        symbol=request.symbol,
                        duration=request.duration,
                        bar_size=request.bar_size,
                        exchange=request.exchange
                    )
                ),
                timeout=30.0  # 30-second timeout for chart data
            )
        except asyncio.TimeoutError:
            logger.error(f"Chart data request timed out for {request.symbol}")
            raise HTTPException(status_code=408, detail="Chart data request timed out")
        
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
    if is_mock_mode():
        try:
            params = {"symbol": symbol, "exchange": exchange}
            async with httpx.AsyncClient() as client:
                response = await client.get(f"{MOCK_BASE}/realtime_data/{symbol}", params={"exchange": exchange})
                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 404:
                    raise HTTPException(status_code=404, detail="No data available")
                else:
                    raise HTTPException(status_code=response.status_code, detail=response.text)
        except httpx.RequestError as e:
            logger.error(f"Mock server request failed: {e}")
            raise HTTPException(status_code=503, detail="Mock server unavailable")
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Mock real-time data error: {e}")
            raise HTTPException(status_code=500, detail=str(e))

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
    if is_mock_mode():
        try:
            # Convert OrderRequest to dict for mock server
            order_data = {
                "symbol": order.symbol,
                "quantity": order.quantity,
                "action": order.action,
                "order_type": order.order_type,
                "exchange": order.exchange,
                "sec_type": order.sec_type,
                "price": order.price
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.post(f"{MOCK_BASE}/place_order", json=order_data)
                if response.status_code == 200:
                    return response.json()
                else:
                    error_detail = response.text if response.text else f"Mock server error: {response.status_code}"
                    raise HTTPException(status_code=response.status_code, detail=error_detail)
                    
        except httpx.RequestError as e:
            logger.error(f"Mock server request failed: {e}")
            raise HTTPException(status_code=503, detail="Mock server unavailable")
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Mock order placement error: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    if not ibkr_client or not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    
    try:
        # 直接调用下单方法，避免嵌套的事件循环调用
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
    if is_mock_mode():
        try:
            async with httpx.AsyncClient() as client:
                response = await client.delete(f"{MOCK_BASE}/cancel_order/{order_id}")
                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 404:
                    raise HTTPException(status_code=404, detail="Order not found")
                else:
                    raise HTTPException(status_code=response.status_code, detail=response.text)
        except httpx.RequestError as e:
            logger.error(f"Mock server request failed: {e}")
            raise HTTPException(status_code=503, detail="Mock server unavailable")
    
    if not ibkr_client or not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    
    try:
        # Run in executor to avoid blocking event loop
        # 直接调用取消订单方法，避免嵌套的事件循环调用
        success, message = ibkr_client.cancel_order(order_id)
        
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
    if is_mock_mode():
        try:
            order_id = request.get('order_id')
            if not order_id:
                raise HTTPException(status_code=400, detail="order_id is required")
            async with httpx.AsyncClient() as client:
                response = await client.post(f"{MOCK_BASE}/cancel_order", json={"order_id": int(order_id)})
                if response.status_code == 200:
                    return response.json()
                else:
                    return {"success": False, "error": response.text}
        except httpx.RequestError as e:
            logger.error(f"Mock server request failed: {e}")
            return {"success": False, "error": "Mock server unavailable"}
        except Exception as e:
            logger.error(f"Cancel order (mock) error: {e}")
            return {"success": False, "error": str(e)}
    
    if not ibkr_client or not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    
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
                    lambda: ibkr_client.cancel_order(int(order_id))
                ),
                timeout=20.0  # 20-second timeout for order cancellation
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

@app.delete("/api/cancel_all_orders")
async def cancel_all_orders(user: Optional[str] = Depends(get_current_user)):
    """取消所有订单"""
    if is_mock_mode():
        try:
            async with httpx.AsyncClient() as client:
                response = await client.delete(f"{MOCK_BASE}/cancel_all_orders")
                if response.status_code == 200:
                    return response.json()
                else:
                    raise HTTPException(status_code=response.status_code, detail=response.text)
        except httpx.RequestError as e:
            logger.error(f"Mock server request failed: {e}")
            raise HTTPException(status_code=503, detail="Mock server unavailable")
    
    if not ibkr_client or not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    
    try:
        # 直接调用取消所有订单方法，避免嵌套的事件循环调用
        cancelled_count = ibkr_client.cancel_all_orders()
        
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
    if is_mock_mode():
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(f"{MOCK_BASE}/orders")
                if response.status_code == 200:
                    return response.json()
                else:
                    raise HTTPException(status_code=response.status_code, detail=response.text)
        except httpx.RequestError as e:
            logger.error(f"Mock server request failed: {e}")
            raise HTTPException(status_code=503, detail="Mock server unavailable")
    
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
    if is_mock_mode():
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(f"{MOCK_BASE}/account_info")
                if response.status_code == 200:
                    return response.json()
                else:
                    raise HTTPException(status_code=response.status_code, detail=response.text)
        except httpx.RequestError as e:
            logger.error(f"Mock server request failed: {e}")
            raise HTTPException(status_code=503, detail="Mock server unavailable")
    
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
async def get_positions(user: Optional[str] = Depends(get_current_user), fast: bool = True):
    """获取持仓列表
    - fast: 是否启用快速模式（默认 True）。快速模式下跳过实时价格抓取，使用缓存/最近价格以提升响应速度。
    """
    if is_mock_mode():
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(f"{MOCK_BASE}/positions", params={"fast": fast})
                if response.status_code == 200:
                    return response.json()
                else:
                    raise HTTPException(status_code=response.status_code, detail=response.text)
        except httpx.RequestError as e:
            logger.error(f"Mock server request failed: {e}")
            raise HTTPException(status_code=503, detail="Mock server unavailable")
    
    if not ibkr_client or not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    
    try:
        # Run in executor with timeout to avoid blocking event loop
        loop = asyncio.get_event_loop()
        try:
            positions = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: ibkr_client.get_positions(fast=fast)),
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

@app.get("/api/strategy/files")
async def list_strategy_files(user: Optional[str] = Depends(get_current_user)):
    """Get list of available strategy files from strategy directory"""
    if not strategy_manager:
        raise HTTPException(status_code=500, detail="Strategy manager not initialized")
    try:
        files = []
        strategy_dir = strategy_manager.strategy_dir
        for file_path in strategy_dir.glob("*.py"):
            if file_path.is_file():
                files.append(file_path.name)
        return {"success": True, "files": files}
    except Exception as e:
        logger.error(f"Failed to list strategy files: {e}")
        raise HTTPException(status_code=500, detail=str(e))

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
    
    # 确保队列worker已启动
    await queue_manager.start_worker()
    
    task_id = queue_manager.add_task("strategy_order", task_payload)
    
    # 立即返回200
    return {
        "success": True,
        "task_id": task_id,
        "message": "Strategy order queued for processing",
        "status": "queued"
    }

# 新增：Mock端点 - 手动触发策略（仅Mock模式）
class MockTriggerRequest(BaseModel):
    """在 Mock 模式下手动触发内部策略执行的请求体
    
    示例1：以 Buy down and Sell open 策略在 MNQ 上买入 1 手市价单
    {
      "strategy": "Buy down and Sell open",
      "symbol": "MNQ",
      "action": "BUY",
      "qty": 1,
      "type": "MARKET",
      "price": null,
      "set_price": false,
      "exchange": "SMART",
      "sec_type": "FUT"
    }
    
    示例2：以 Buy the Dip Strategy 在 MNQ 上挂 17950 的限价买入 1 手，并先设置价格
    {
      "strategy": "Buy the Dip Strategy",
      "symbol": "MNQ",
      "action": "BUY",
      "qty": 1,
      "type": "LIMIT",
      "price": 17950,
      "set_price": true,
      "exchange": "SMART",
      "sec_type": "FUT"
    }
    """
    strategy: str
    symbol: str
    action: str  # BUY, SELL
    qty: int = 1
    type: str = "MARKET"  # MARKET, LIMIT
    price: Optional[float] = None
    set_price: bool = False  # 若提供价格，是否先设置Mock价格
    exchange: str = "SMART"
    sec_type: str = "STK"

@app.post("/api/mock/trigger_strategy")
async def mock_trigger_strategy(payload: MockTriggerRequest, user: Optional[str] = Depends(get_current_user)):
    """在 Mock 模式下手动触发内部策略执行
    - 可选：若提供 price 且 set_price=True，先调用 Mock 网关设置最新价
    - 然后将任务加入与外部策略一致的队列，异步处理下单
    
    成功返回示例：
    {
      "success": true,
      "task_id": "abc123",
      "message": "Mock trigger queued for processing",
      "status": "queued",
      "mock_price_set": true
    }
    """
    # 仅在Mock模式下可用
    if not is_mock_mode():
        raise HTTPException(status_code=400, detail="This endpoint is only available in MOCK mode")

    if not strategy_manager:
        raise HTTPException(status_code=500, detail="Strategy manager not initialized")

    # 校验策略存在且已启用
    strategy = strategy_manager.get_strategy(payload.strategy)
    if not strategy or not strategy.get('enabled', False):
        raise HTTPException(status_code=400, detail="Strategy disabled or not found")

    # 可选：先设置Mock价格
    mock_price_set = False
    if payload.set_price and payload.price is not None:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(f"{MOCK_BASE}/mock/set_price", json={"symbol": payload.symbol, "price": payload.price})
                if resp.status_code == 200:
                    mock_price_set = True
                else:
                    logger.warning(f"Mock set_price failed: {resp.status_code} {resp.text}")
        except Exception as e:
            logger.warning(f"Mock set_price exception: {e}")

    # 构建并入队任务（与 /api/strategy 一致）
    task_payload = {
        "strategy": payload.strategy,
        "symbol": payload.symbol,
        "action": payload.action,
        "qty": payload.qty,
        "type": payload.type,
        "price": payload.price,
        "exchange": payload.exchange,
        "sec_type": payload.sec_type,
    }

    # 确保队列worker已启动
    await queue_manager.start_worker()

    task_id = queue_manager.add_task("strategy_order", task_payload)

    return {
        "success": True,
        "task_id": task_id,
        "message": "Mock trigger queued for processing",
        "status": "queued",
        "mock_price_set": mock_price_set,
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
async def get_logs(
    lines: int = 100,
    level: str = "all",
    request_id: Optional[str] = None,
    module: str = "all",
    range: str = "today",
    user: Optional[str] = Depends(get_current_user),
):
    try:
        log_file = "logs/trading_bot.log"
        if not os.path.exists(log_file):
            return {"success": True, "logs": []}
        with open(log_file, "r") as f:
            content = f.readlines()

        # Filter by date range
        now = datetime.now()
        if range == "today":
            date_str = now.strftime("%Y-%m-%d")
            content = [line for line in content if line.startswith(f"| {date_str}")]
        elif range == "week":
            start = now - timedelta(days=now.weekday())
            filtered = []
            for line in content:
                try:
                    ts = datetime.strptime(line[2:21], "%Y-%m-%d %H:%M:%S")
                except Exception:
                    continue
                if ts.date() >= start.date():
                    filtered.append(line)
            content = filtered

        if level.lower() != "all":
            content = [line for line in content if f"| {level.upper()} |" in line]

        if module != "all":
            content = [line for line in content if f"| {module} |" in line]

        if request_id:
            content = [line for line in content if request_id in line]

        return {"success": True, "logs": content[-lines:]}
    except Exception as e:
        logger.error(f"Get logs error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/logs/stats")
async def get_log_stats(
    range: str = "today",
    module: str = "all",
    user: Optional[str] = Depends(get_current_user),
):
    try:
        log_file = "logs/trading_bot.log"
        if not os.path.exists(log_file):
            return {"success": True, "stats": {}}
        with open(log_file, "r") as f:
            lines = f.readlines()

        now = datetime.now()
        if range == "today":
            date_str = now.strftime("%Y-%m-%d")
            lines = [line for line in lines if line.startswith(f"| {date_str}")]
        elif range == "week":
            start = now - timedelta(days=now.weekday())
            filtered = []
            for line in lines:
                try:
                    ts = datetime.strptime(line[2:21], "%Y-%m-%d %H:%M:%S")
                except Exception:
                    continue
                if ts.date() >= start.date():
                    filtered.append(line)
            lines = filtered

        pattern = re.compile(r"REQUEST \w+ (?P<path>\S+) status=(?P<status>\d+) duration=(?P<duration>[0-9.]+)ms")
        stats: Dict[str, Dict[str, Dict[str, float]]] = {}

        for line in lines:
            parts = line.split("|")
            if len(parts) < 6:
                continue
            mod = parts[3].strip()
            if module != "all" and mod != module:
                continue
            m = pattern.search(line)
            if not m:
                continue
            path = m.group("path")
            status = int(m.group("status"))
            duration = float(m.group("duration"))
            mod_stats = stats.setdefault(mod, {})
            entry = mod_stats.setdefault(path, {"count": 0, "total_time": 0.0, "success": 0})
            entry["count"] += 1
            entry["total_time"] += duration
            if status < 400:
                entry["success"] += 1

        for mod, paths in stats.items():
            for path, data in paths.items():
                data["avg_time"] = round(data["total_time"] / data["count"], 2)
                data["success_rate"] = round(data["success"] / data["count"], 2)
                del data["total_time"]
                del data["success"]

        return {"success": True, "stats": stats}
    except Exception as e:
        logger.error(f"Get log stats error: {e}")
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
    
    # Auto connect if enabled in .env and not in mock mode
    auto_connect = os.getenv('AUTO_CONNECT', 'False').lower() == 'true'
    if auto_connect and not is_mock_mode():
        logger.info("Auto-connect enabled, attempting to connect to IBKR...")
        try:
            # Use the connection manager directly for synchronous connection test
            success = connection_manager.connect()
            if not success:
                logger.warning("IBKR auto-connection failed. Continuing without connection...")
                logger.info("You can connect later through the web interface.")
            else:
                logger.info("IBKR auto-connection successful!")
                # Initialize ibkr_client when auto-connect succeeds
                global ibkr_client
                if ibkr_client is None:
                    ibkr_client = IBKRClient()
                    logger.info("IBKRClient initialized after auto-connect")
                    
                    # Update risk_manager with ibkr_client
                    try:
                        risk_manager.ibkr_client = ibkr_client
                        logger.info("Risk manager ibkr_client updated with auto-connected client")
                    except Exception as re_err:
                        logger.warning(f"RiskManager ibkr_client update warning: {re_err}")
                    
                    # Update queue_manager dependencies
                    try:
                        queue_manager.set_dependencies(
                            ibkr_client=ibkr_client,
                            risk_manager=risk_manager,
                            strategy_manager=strategy_manager,
                            connection_manager=connection_manager,
                            websocket_manager=websocket_manager,
                            order_manager=order_manager
                        )
                        logger.info("Queue manager dependencies updated after auto-connect")
                    except Exception as dep_err:
                        logger.warning(f"QueueManager set_dependencies warning: {dep_err}")
        except Exception as e:
            logger.warning(f"Auto-connection failed: {e}")
            logger.info("You can connect later through the web interface.")
    elif auto_connect and is_mock_mode():
        logger.info("Auto-connect enabled but running in mock mode - skipping real IBKR connection")
    else:
        logger.info("Auto-connect disabled. You can connect later through the web interface.")
    
    # Start web server
    logger.info("Starting web server...")
    web_host = os.getenv('WEB_HOST', '0.0.0.0')
    web_port = int(os.getenv('WEB_PORT', '8000'))
    display_host = 'localhost' if web_host in ('0.0.0.0', '::') else web_host
    logger.info(f"Web UI available at: http://{display_host}:{web_port}")
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