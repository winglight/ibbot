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
import pandas as pd

# FastAPI imports
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Depends, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel
from jose import JWTError, jwt
from starlette.middleware.gzip import GZipMiddleware

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
from src.models import HistoricalDataRequest, ChartDataRequest, OrderRequest, ExternalStrategyOrderRequest, StrategySettingsRequest, StrategyRequest, RiskRuleRequest, RiskConfigUpdateRequest, PositionSizingRequest, CurrentPriceRequest
from src.websocket_manager import websocket_manager  # 新增导入
from src.notification_manager import NotificationManager

# Global variables
load_dotenv()

# 优化：可配置的PnL广播间隔（秒），默认5秒
PNL_BROADCAST_INTERVAL_SEC = int(os.getenv('PNL_BROADCAST_INTERVAL_SEC', '5'))

# 内存缓存：用于缓存历史数据请求
# 格式: {"symbol_duration_barsize": {"data": df, "timestamp": datetime, "expires_at": datetime}}
historical_data_cache: Dict[str, Dict[str, Any]] = {}
CACHE_DURATION_MINUTES = 1  # 缓存1分钟

# Context variables for request-scoped logging
request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
module_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("module", default="-")


# Timezone helpers
LOCAL_TZ = datetime.now().astimezone().tzinfo
NY_TZ = ZoneInfo("America/New_York")


def to_local_time(value: Any) -> str:
    """Convert IBKR timestamps (US/Eastern) to local timezone ISO strings."""
    try:
        if hasattr(value, "to_pydatetime"):
            value = value.to_pydatetime()
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, str):
            raw = value.strip()
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y%m%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(raw, fmt)
                    break
                except ValueError:
                    dt = None
            if dt is None:
                dt = datetime.fromisoformat(raw)
        else:
            return str(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=NY_TZ)
        else:
            dt = dt.astimezone(NY_TZ)
        return dt.astimezone(LOCAL_TZ).isoformat()
    except Exception:
        return str(value)


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
                    # Initialize ibkr_client when auto-connect succeeds
                    global ibkr_client
                    if ibkr_client is None:
                        ibkr_client = IBKRClient()
                        main_logger.info("IBKRClient initialized after auto-connect")
                        
                        # Update risk_manager with ibkr_client
                        try:
                            risk_manager.ibkr_client = ibkr_client
                            main_logger.info("Risk manager ibkr_client updated with auto-connected client")
                        except Exception as re_err:
                            main_logger.warning(f"RiskManager ibkr_client update warning: {re_err}")
                        
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
    
    yield
    # Shutdown
    main_logger.info("Application shutting down...")

app = FastAPI(lifespan=lifespan)

# 启用GZip压缩，减小大文件(如 app.js)传输体积
app.add_middleware(GZipMiddleware, minimum_size=1024)

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


def _parse_duration_to_minutes(duration: str) -> int:
    """解析duration字符串为分钟数，支持D/day, H/hour, min/minute, W/week"""
    if not duration:
        return 0
    s = str(duration).strip().lower()
    parts = s.split()
    if not parts:
        return 0
    try:
        n = float(parts[0])
    except Exception:
        # 尝试从前缀解析数字，如 "30min"
        import re as _re
        m = _re.match(r"(\d+(?:\.\d+)?)([a-zA-Z]+)", s)
        if m:
            n = float(m.group(1))
            unit = m.group(2)
        else:
            return 0
    else:
        unit = parts[1] if len(parts) > 1 else 'min'
    unit = unit.strip().lower()
    if unit in ('d', 'day', 'days'):
        return int(n * 1440)
    if unit in ('h', 'hour', 'hours'):
        return int(n * 60)
    if unit in ('w', 'week', 'weeks'):
        return int(n * 7 * 1440)
    if unit in ('m', 'min', 'mins', 'minute', 'minutes'):
        return int(n)
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
notification_manager = NotificationManager(telegram_notifier=telegram_notifier, websocket_manager=websocket_manager, db_manager=get_db_manager())  # 统一通知管理器
risk_manager = RiskManager(ibkr_client=None, notification_manager=notification_manager)  # 使用NotificationManager
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

# 重新创建带有lifespan的FastAPI应用
app = FastAPI(title="IBKR Trading Bot", version="1.0.0", lifespan=lifespan)

# Re-attach middleware, static, and docs routes to the new app instance
app.middleware("http")(request_id_middleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
app.add_api_route("/", index_page)
app.add_api_route("/api-docs.json", get_api_docs_main, include_in_schema=False)
app.add_api_route("/rapidoc", rapidoc_html_main, include_in_schema=False)
# Ensure /api/login is registered on the re-created app instance
app.add_api_route("/api/login", login, methods=["POST"])
# Re-apply custom OpenAPI generator
app.openapi = custom_openapi

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

# 实时行情推送任务
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

# FastAPI 生命周期事件已移至 lifespan 函数中

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

# 为 OrderManager 注入依赖
try:
    order_manager.set_dependencies(
        websocket_manager=websocket_manager,
        notification_manager=notification_manager,
        risk_manager=risk_manager
    )
except Exception as _dep_err:
    logger.warning(f"OrderManager set_dependencies at startup failed: {_dep_err}")

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
    """连接到IBKR"""
    global ibkr_client

    try:
        # 更新配置
        connection_manager.update_config(config)

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


class NotificationRequest(BaseModel):
    message: str


class NotificationTestRequest(BaseModel):
    channel: str
    message: str = "Test notification from IBKR Trading Bot"


@app.post("/api/notify")
async def send_notification(req: NotificationRequest, user: Optional[str] = Depends(get_current_user)):
    """发送Telegram通知"""
    try:
        success = await notification_manager.send(req.message, "api", "test")
        if not success:
            # 返回200状态码但标记为失败，避免前端报错
            return {"success": False, "message": "Notification disabled or failed to send"}
        return {"success": True}
    except Exception as e:
        logger.error(f"Error sending notification: {e}")
        # 返回200状态码但包含错误信息，避免前端报错
        return {"success": False, "message": f"Error: {str(e)}"}


# Market data endpoints
def _clean_expired_cache():
    """清理过期的内存缓存"""
    global historical_data_cache
    now = datetime.now()
    expired_keys = [key for key, value in historical_data_cache.items() 
                   if value.get('expires_at', now) <= now]
    for key in expired_keys:
        del historical_data_cache[key]
    if expired_keys:
        logger.debug(f"Cleaned {len(expired_keys)} expired cache entries")

@app.post("/api/historical_data")
async def get_historical_data(request: HistoricalDataRequest, user: Optional[str] = Depends(get_current_user)):
    """获取历史数据"""
    if not ibkr_client or not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")

    try:
        # 清理过期缓存
        _clean_expired_cache()
        
        # 生成缓存键
        cache_key = f"{request.symbol}_{request.duration}_{request.bar_size}_{request.exchange}"
        now = datetime.now()
        
        # 先检查内存缓存
        if cache_key in historical_data_cache:
            cache_entry = historical_data_cache[cache_key]
            if cache_entry.get('expires_at', now) > now:
                logger.debug(f"Using memory cache for {request.symbol}")
                cached_data = cache_entry['data']
                return {
                    "success": True,
                    "data": cached_data,
                    "filename": f"data/{request.symbol}_historical.csv",
                    "rows": len(cached_data),
                    "source": "memory_cache"
                }
        
        # 再查数据库缓存
        db_manager = get_db_manager()
        interval = db_manager._normalize_interval(request.bar_size)
        minutes = _parse_duration_to_minutes(request.duration)
        start_dt = now - timedelta(minutes=minutes)

        # 读取缓存
        cache_df = db_manager.query_historical_cache(
            request.symbol,
            interval=interval,
            start_dt=start_dt.isoformat(timespec='seconds'),
            end_dt=now.isoformat(timespec='seconds')
        )

        # 检查缓存是否满足需求
        if not db_manager.detect_missing_ranges(cache_df, start_dt.isoformat(), now.isoformat(), interval):
            # 缓存足够，直接返回
            logger.debug(f"Using database cache for {request.symbol}")
            data_dict = cache_df.to_dict('records')
            
            # 同时保存到内存缓存
            expires_at = now + timedelta(minutes=CACHE_DURATION_MINUTES)
            historical_data_cache[cache_key] = {
                'data': data_dict,
                'timestamp': now,
                'expires_at': expires_at
            }
            
            filename = f"data/{request.symbol}_historical.csv"
            try:
                os.makedirs(os.path.dirname(filename), exist_ok=True)
                cache_df.to_csv(filename, index=False)
            except Exception as e:
                logger.warning(f"Failed to write CSV backup for historical data (cache): {e}")
            return {
                "success": True,
                "data": data_dict,
                "filename": filename,
                "rows": len(cache_df),
                "source": "database_cache"
            }

        # 缓存不完整，从IB获取数据
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
            # 即使IB无数据，也返回缓存（若有）
            if cache_df is not None and not cache_df.empty:
                return {
                    "success": True,
                    "data": cache_df.to_dict('records'),
                    "filename": None,
                    "rows": len(cache_df)
                }
            raise HTTPException(status_code=404, detail="No data found")

        # 保存到数据库
        db_manager.save_historical_data(request.symbol, df, bar_size=request.bar_size)

        # 转换为字典格式
        data_dict = df.to_dict('records')
        
        # 保存到内存缓存
        expires_at = now + timedelta(minutes=CACHE_DURATION_MINUTES)
        historical_data_cache[cache_key] = {
            'data': data_dict,
            'timestamp': now,
            'expires_at': expires_at
        }
        logger.debug(f"Cached historical data for {request.symbol}, expires at {expires_at}")

        # 可选：同时保存CSV作为备份
        filename = f"data/{request.symbol}_historical.csv"
        try:
            os.makedirs(os.path.dirname(filename), exist_ok=True)
            df.to_csv(filename, index=False)
        except Exception as e:
            logger.warning(f"Failed to write CSV backup for historical data: {e}")

        return {
            "success": True,
            "data": data_dict,
            "filename": filename,
            "rows": len(df),
            "source": "ibkr_api"
        }

    except Exception as e:
        logger.error(f"Historical data error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/chart_data")
async def get_chart_data(request: ChartDataRequest, user: Optional[str] = Depends(get_current_user)):
    """获取图表数据（改进错误提示与缓存回退）"""
    if not ibkr_client or not connection_manager.is_connected():
        # 返回更具体的原因（如另一处已登录等）
        reason = None
        try:
            reason = connection_manager.get_last_connect_error()
        except Exception:
            reason = None
        detail = "Not connected to IBKR" + (f": {reason}" if reason else "")
        raise HTTPException(status_code=400, detail=detail)

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
                timeout=65.0  # 略大于连接管理器的60秒超时
            )
        except asyncio.TimeoutError:
            # 检查连接状态
            is_connected = connection_manager.is_connected()
            error_msg = f"Chart data request timed out for {request.symbol}. IBKR connection status: {'Connected' if is_connected else 'Disconnected'}"
            logger.error(error_msg)
            
            # 如果连接断开，返回408错误并提示重新连接
            if not is_connected:
                return JSONResponse(
                    status_code=408,
                    content={"detail": "IBKR connection lost. Please reconnect to IBKR Gateway/TWS and try again.", "error_type": "connection_lost"}
                )
            
            # 超时：尝试数据库缓存回退
            cache_df = None
            try:
                db_manager = get_db_manager()
                cache_df = db_manager.load_historical_data(request.symbol, bar_size=request.bar_size)
            except Exception:
                cache_df = None
            if cache_df is not None and not cache_df.empty:
                chart_data = []
                try:
                    for ts, row in cache_df.iterrows():
                        t = to_local_time(ts)
                        chart_data.append({
                            "time": t,
                            "open": float(row.get('open', 0) or 0),
                            "high": float(row.get('high', 0) or 0),
                            "low": float(row.get('low', 0) or 0),
                            "close": float(row.get('close', 0) or 0),
                            "volume": int(row.get('volume', 0) or 0)
                        })
                except Exception:
                    chart_data = []
                if chart_data:
                    return {
                        "success": True,
                        "data": chart_data,
                        "symbol": request.symbol,
                        "duration": request.duration,
                        "bar_size": request.bar_size
                    }
            # 无缓存：返回 408 并附带最近连接错误原因
            reason = None
            try:
                reason = connection_manager.get_last_connect_error()
            except Exception:
                reason = None
            msg = "Chart data request timed out" + (f". Possible cause: {reason}" if reason else "")
            raise HTTPException(status_code=408, detail=msg)

        if df is not None and not df.empty:
            db_manager = get_db_manager()
            try:
                db_manager.save_historical_data(request.symbol, df, bar_size=request.bar_size)
            except Exception as e:
                logger.warning(f"Failed to save chart data: {e}")

            chart_data = []
            for _, row in df.iterrows():
                raw_time = row['date']
                if 'time' in df.columns and not pd.isna(row.get('time')):
                    raw_time = f"{row['date']} {row['time']}"
                t = to_local_time(raw_time)
                chart_data.append({
                    "time": t,
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

        # df 为空：尝试数据库缓存回退
        cache_df = None
        try:
            db_manager = get_db_manager()
            cache_df = db_manager.load_historical_data(request.symbol, bar_size=request.bar_size)
        except Exception:
            cache_df = None
        if cache_df is not None and not cache_df.empty:
            chart_data = []
            try:
                for ts, row in cache_df.iterrows():
                    t = to_local_time(ts)
                    chart_data.append({
                        "time": t,
                        "open": float(row.get('open', 0) or 0),
                        "high": float(row.get('high', 0) or 0),
                        "low": float(row.get('low', 0) or 0),
                        "close": float(row.get('close', 0) or 0),
                        "volume": int(row.get('volume', 0) or 0)
                    })
            except Exception:
                chart_data = []
            if chart_data:
                return {
                    "success": True,
                    "data": chart_data,
                    "symbol": request.symbol,
                    "duration": request.duration,
                    "bar_size": request.bar_size
                }
        # 无缓存：返回 404 并附带最近连接错误原因
        reason = None
        try:
            reason = connection_manager.get_last_connect_error()
        except Exception:
            reason = None
        raise HTTPException(status_code=404, detail=("No chart data found" + (f". Possible cause: {reason}" if reason else "")))

    except HTTPException as http_exc:
        # 保持原有的状态码与信息
        raise http_exc
    except Exception as e:
        logger.error(f"Chart data error: {e}")
        # 500 错误附带最近连接错误原因
        reason = None
        try:
            reason = connection_manager.get_last_connect_error()
        except Exception:
            reason = None
        detail = (str(e) if str(e) else "Internal error") + (f" | {reason}" if reason else "")
        raise HTTPException(status_code=500, detail=detail)

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

@app.post("/api/current_price")
async def get_current_price(request: CurrentPriceRequest, user: Optional[str] = Depends(get_current_user)):
    """获取当前价格 - 轻量级接口，专门用于获取最新价格"""
    if not ibkr_client or not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    
    try:
        # 首先尝试从价格缓存获取
        if hasattr(ibkr_client, 'price_cache') and request.symbol in ibkr_client.price_cache:
            cached_price = ibkr_client.price_cache.get(request.symbol)
            if cached_price:
                return {
                    "success": True,
                    "symbol": request.symbol,
                    "price": cached_price,
                    "timestamp": datetime.now().isoformat(),
                    "source": "cache"
                }
        
        # 如果缓存中没有，尝试获取实时数据
        loop = asyncio.get_event_loop()
        try:
            data = await asyncio.wait_for(
                loop.run_in_executor(
                    None, 
                    lambda: ibkr_client.get_real_time_data(request.symbol, request.exchange)
                ), 
                timeout=5.0  # 5秒超时，比realtime_data接口更短
            )
            
            if data and 'last' in data:
                price = data['last']
                return {
                    "success": True,
                    "symbol": request.symbol,
                    "price": price,
                    "timestamp": datetime.now().isoformat(),
                    "source": "realtime"
                }
        except asyncio.TimeoutError:
            logger.warning(f"Current price request for {request.symbol} timed out")
        
        # 最后尝试从历史数据获取最新价格（1分钟数据）
        try:
            df = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: ibkr_client.get_historical_data(
                        symbol=request.symbol,
                        duration="1 D",
                        bar_size="1 min",
                        exchange=request.exchange
                    )
                ),
                timeout=10.0
            )
            
            if df is not None and not df.empty:
                latest_price = float(df.iloc[-1]['close'])
                return {
                    "success": True,
                    "symbol": request.symbol,
                    "price": latest_price,
                    "timestamp": datetime.now().isoformat(),
                    "source": "historical"
                }
        except asyncio.TimeoutError:
            logger.warning(f"Historical data fallback for {request.symbol} timed out")
        
        raise HTTPException(status_code=404, detail="Unable to get current price")
        
    except Exception as e:
        logger.error(f"Current price error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Trading endpoints
@app.post("/api/place_order")
async def place_order(order: OrderRequest, user: Optional[str] = Depends(get_current_user)):
    """下单"""
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
        # Run in executor to avoid blocking event loop with timeout
        loop = asyncio.get_event_loop()
        try:
            success, message = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: ibkr_client.cancel_order(order_id)
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

@app.post("/api/close_position")
async def close_position(request: dict, user: Optional[str] = Depends(get_current_user)):
    """平仓接口 - 根据持仓情况自动计算平仓订单"""
    symbol = request.get("symbol")
    if not symbol:
        raise HTTPException(status_code=400, detail="Symbol is required")
    
    if not ibkr_client or not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    
    try:
        # 获取当前持仓
        positions = await ibkr_client.get_positions()
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
        trade = ibkr_client.place_market_order(
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

@app.post("/api/orders/validate")
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
    
    if not ibkr_client:
        errors.append("IBKR client not initialized")
    elif not connection_manager.is_connected():
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
        if ibkr_client and connection_manager.is_connected():
            # 获取当前持仓
            positions = await ibkr_client.get_positions()
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
        if ibkr_client and connection_manager.is_connected():
            # 获取账户信息
            account_info = ibkr_client.get_account_summary()
            
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

@app.post("/api/reverse_position")
async def reverse_position(request: dict, user: Optional[str] = Depends(get_current_user)):
    """反向下单接口 - 根据持仓情况自动计算反向订单"""
    symbol = request.get("symbol")
    if not symbol:
        raise HTTPException(status_code=400, detail="Symbol is required")
    
    if not ibkr_client or not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    
    try:
        # 获取当前持仓
        positions = await ibkr_client.get_positions()
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
                        from datetime import datetime
                        db_rule = DatabaseRiskRule(
                            symbol=rule.symbol,
                            stop_loss_price=rule.stop_loss_price,
                            take_profit_price=rule.take_profit_price,
                            max_loss_percent=rule.max_loss_percent,
                            enabled=rule.enabled,
                            timestamp=rule.created_at.isoformat() if rule.created_at else datetime.now().isoformat()
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
        trade = ibkr_client.place_market_order(
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

@app.delete("/api/cancel_all_orders")
async def cancel_all_orders(user: Optional[str] = Depends(get_current_user)):
    """取消所有订单"""
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

# 订单缓存，避免频繁查询数据库
orders_cache = []
orders_cache_timestamp = None
ORDERS_CACHE_TTL = 3  # 缓存有效期（秒）

@app.get("/api/orders")
async def get_orders(user: Optional[str] = Depends(get_current_user), limit: int = 100, use_cache: bool = True):
    """获取订单列表（从数据库读取，包含历史订单）
    - limit: 限制返回的订单数量，默认100条，提高性能
    - use_cache: 是否使用API级别缓存（默认 True）。启用时，短时间内的重复请求将直接返回缓存结果，无需重新查询数据库。
    """
    global orders_cache, orders_cache_timestamp
    
    try:
        if 'order_manager' not in globals() or not order_manager:
            raise HTTPException(status_code=500, detail="Order manager not initialized")
        
        # 检查缓存是否有效
        current_time = datetime.now()
        if use_cache and orders_cache and orders_cache_timestamp:
            cache_age = (current_time - orders_cache_timestamp).total_seconds()
            if cache_age < ORDERS_CACHE_TTL:
                logger.debug(f"Using orders cache, age: {cache_age:.1f}s")
                return {"success": True, "orders": orders_cache, "from_cache": True}
        
        # 从 OrderManager 获取所有订单数据（已按创建时间降序排列），限制数量提高性能
        # 使用异步执行避免阻塞事件循环
        loop = asyncio.get_event_loop()
        try:
            orders = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: order_manager.get_orders(limit=limit)),
                timeout=5.0  # 5秒超时
            )
            
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
            
            # 更新缓存
            orders_cache = converted_orders
            orders_cache_timestamp = current_time
            
            return {"success": True, "orders": converted_orders, "from_cache": False}
            
        except asyncio.TimeoutError:
            # 如果超时但有缓存，返回缓存数据
            if orders_cache:
                logger.warning("Orders request timed out, returning cached data")
                return {"success": True, "orders": orders_cache, "from_cache": True, "cache_warning": "Request timed out, showing cached data"}
            else:
                logger.warning("Orders request timed out and no cache available")
                return {"success": False, "error": "Orders request timed out"}
        
    except HTTPException:
        raise
    except Exception as e:
        # 如果出错但有缓存，返回缓存数据
        if orders_cache and orders_cache_timestamp:
            logger.error(f"Get orders error: {e}, returning cached data")
            return {"success": True, "orders": orders_cache, "from_cache": True, "cache_warning": "Error occurred, showing cached data"}
        
        logger.error(f"Get orders error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/orders/{order_id}")
async def get_order_detail(order_id: int, user: Optional[str] = Depends(get_current_user)):
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
            'submit_time': (o.created_at.isoformat() if getattr(o, 'created_at', None) else None),
            'order_source': getattr(o, 'order_source', 'manual'),
            'trigger_type': trigger_type,
            # 详情字段
            'price': price_val if price_val > 0 else None,
            'executed_at': (o.executed_at.isoformat() if getattr(o, 'executed_at', None) else None),
            'updated_at': (o.updated_at.isoformat() if getattr(o, 'updated_at', None) else None),
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

@app.post("/api/orders/sync")
async def sync_orders(user: Optional[str] = Depends(get_current_user)):
    """手动同步订单状态和价格字段从IB"""
    if not ibkr_client or not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    
    try:
        if 'order_manager' not in globals() or not order_manager:
            raise HTTPException(status_code=500, detail="Order manager not initialized")
        
        # 手动同步订单状态
        loop = asyncio.get_event_loop()
        await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: ibkr_client.get_orders(risk_manager, strategy_manager, order_manager)
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

@app.post("/api/orders/{order_id}/sync")
async def sync_single_order(order_id: int, user: Optional[str] = Depends(get_current_user)):
    """同步单个订单状态"""
    if not ibkr_client or not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    
    try:
        if 'order_manager' not in globals() or not order_manager:
            raise HTTPException(status_code=500, detail="Order manager not initialized")
        
        # 手动同步订单状态
        loop = asyncio.get_event_loop()
        await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: ibkr_client.get_orders(risk_manager, strategy_manager, order_manager)
            ),
            timeout=10.0
        )
        
        return {"success": True, "message": f"Order {order_id} synced successfully"}
        
    except asyncio.TimeoutError:
        logger.warning(f"Order {order_id} sync request timed out")
        raise HTTPException(status_code=408, detail="Order sync request timed out")
    except Exception as e:
        logger.error(f"Sync order {order_id} error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Account endpoints
@app.get("/api/account_info")
async def get_account_info(user: Optional[str] = Depends(get_current_user)):
    """获取账户信息"""
    if not ibkr_client or not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    
    try:
        # Direct async call with timeout
        try:
            info = await asyncio.wait_for(
                ibkr_client.get_account_info(),
                timeout=10.0  # 10-second timeout
            )
        except asyncio.TimeoutError:
            logger.warning("Account info request timed out")
            return {"success": False, "error": "Account info request timed out"}
            
        return {"success": True, "account_info": info}
        
    except Exception as e:
        logger.error(f"Get account info error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# 持仓缓存，避免频繁请求IBKR API
positions_cache = {}
positions_cache_timestamp = None
POSITIONS_CACHE_TTL = 5  # 缓存有效期（秒）

@app.get("/api/positions")
async def get_positions(user: Optional[str] = Depends(get_current_user), fast: bool = True, use_cache: bool = True):
    """获取持仓列表
    - fast: 是否启用快速模式（默认 True）。快速模式下跳过实时价格抓取，使用缓存/最近价格以提升响应速度。
    - use_cache: 是否使用API级别缓存（默认 True）。启用时，短时间内的重复请求将直接返回缓存结果，无需重新查询IBKR。
    """
    global positions_cache, positions_cache_timestamp
    
    # 如果未连接IB，返回空持仓列表而不是报错
    if not ibkr_client or not connection_manager.is_connected():
        logger.warning("Not connected to IBKR, returning empty positions list")
        return {"success": True, "positions": [], "from_cache": False, "warning": "Not connected to IBKR"}
    
    try:
        # 检查缓存是否有效
        current_time = datetime.now()
        if use_cache and positions_cache and positions_cache_timestamp:
            cache_age = (current_time - positions_cache_timestamp).total_seconds()
            if cache_age < POSITIONS_CACHE_TTL:
                logger.debug(f"Using positions cache, age: {cache_age:.1f}s")
                return {"success": True, "positions": positions_cache, "from_cache": True}
        
        # 直接使用异步方法，避免阻塞事件循环
        try:
            positions = await asyncio.wait_for(
                ibkr_client.get_positions(fast=fast),
                timeout=5.0  # 缩短超时时间到5秒
            )
            
            # 更新缓存
            positions_cache = positions
            positions_cache_timestamp = current_time
            
            return {"success": True, "positions": positions, "from_cache": False}
            
        except asyncio.TimeoutError:
            # 如果超时但有缓存，返回缓存数据
            if positions_cache:
                logger.warning("Positions request timed out, returning cached data")
                return {"success": True, "positions": positions_cache, "from_cache": True, "cache_warning": "Request timed out, showing cached data"}
            else:
                logger.warning("Positions request timed out and no cache available")
                return {"success": False, "error": "Positions request timed out"}
        
    except Exception as e:
        # 如果出错但有缓存，返回缓存数据
        if positions_cache:
            logger.error(f"Get positions error: {e}, returning cached data")
            return {"success": True, "positions": positions_cache, "from_cache": True, "cache_warning": "Error occurred, showing cached data"}
        
        logger.error(f"Get positions error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Risk management endpoints
@app.post("/api/risk_rules")
async def add_risk_rule(rule: RiskRuleRequest, user: Optional[str] = Depends(get_current_user)):
    """添加风险管理规则"""
    if not risk_manager:
        raise HTTPException(status_code=400, detail="Risk manager not initialized")
    
    try:
        logger.info(f"API add_risk_rule: symbol={rule.symbol}, SL={rule.stop_loss_price}, TP={rule.take_profit_price}, max_loss%={rule.max_loss_percent}, max_time_span={rule.max_time_span}")
        risk_manager.add_risk_rule(
            symbol=rule.symbol,
            stop_loss_price=rule.stop_loss_price,
            take_profit_price=rule.take_profit_price,
            max_loss_percent=rule.max_loss_percent,
            max_time_span=rule.max_time_span
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
                "max_time_span": rule.max_time_span,
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
        
        leverage = {}
        try:
            default_mnq_lev = int(os.getenv('LEVERAGE_MNQ', 20))
            if 'risk_manager' in globals() and risk_manager is not None:
                leverage["MNQ"] = int(getattr(risk_manager, "leverage_mnq", default_mnq_lev))
            else:
                leverage["MNQ"] = default_mnq_lev
        except Exception:
            leverage["MNQ"] = 20

        # 新增：返回完整风控配置，添加超时保护
        full_cfg = {}
        try:
            if 'risk_manager' in globals() and risk_manager is not None:
                loop = asyncio.get_event_loop()
                full_cfg = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: risk_manager.get_config()),
                    timeout=3.0  # 3秒超时
                )
        except asyncio.TimeoutError:
            logger.warning("get_risk_config: fetch full config timed out")
            full_cfg = {}
        except Exception as e:
            logger.warning(f"get_risk_config: fetch full config failed: {e}")
            full_cfg = {}

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
@app.put("/api/risk-config")
async def update_risk_config(payload: RiskConfigUpdateRequest, user: Optional[str] = Depends(get_current_user)):
    if 'risk_manager' not in globals() or risk_manager is None:
        raise HTTPException(status_code=400, detail="Risk manager not initialized")
    try:
        updates = {k: v for k, v in payload.dict(exclude_none=True).items()}
        new_cfg = risk_manager.update_config(updates)
        return {"success": True, "risk_config": new_cfg}
    except Exception as e:
        logger.error(f"Update risk config error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# 新增：仓位建议计算
# Removed duplicate /api/position-sizing endpoint - use /api/sizing/preview instead


@app.post("/api/position-sizing")
async def api_position_sizing(payload: PositionSizingRequest, user: Optional[str] = Depends(get_current_user)):
    """Position sizing calculation - optimized to only require symbol and stop_distance"""
    if 'risk_manager' not in globals() or risk_manager is None:
        raise HTTPException(status_code=400, detail="Risk manager not initialized")
    
    try:
        # 获取实时账户信息
        account_info = {}
        if ibkr_client and connection_manager.is_connected():
            try:
                account_response = await asyncio.wait_for(
                    ibkr_client.get_account_info(),
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
            res = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: risk_manager.position_sizing(
                        symbol=payload.symbol,
                        stop_distance=payload.stop_distance,
                        rv_current=None,  # 让risk_manager自动估算当前价格
                        signal_tier='A',  # 使用默认信号等级
                        execution_metrics=None,  # 使用默认执行指标
                        rv_ref=None,  # 使用默认参考价格
                    )
                ),
                timeout=10.0  # 10秒超时
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
@app.get("/api/sizing/history")
async def get_sizing_history(symbol: Optional[str] = None, since: Optional[str] = None, limit: int = 100, user: Optional[str] = Depends(get_current_user)):
    try:
        db = get_db_manager()
        items = db.load_sizing_history(symbol=symbol, since=since, limit=limit)
        return {"success": True, "data": items, "count": len(items)}
    except Exception as e:
        logger.error(f"Get sizing history error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# 配置管理接口 - 统一管理G Rules和S Rules
@app.get("/api/config/rules")
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


@app.put("/api/config/rules")
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
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket_manager.connect(websocket)
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

# Notifications API
@app.get("/api/notifications/settings")
async def get_notification_settings(user: Optional[str] = Depends(get_current_user)):
    """获取通知设置"""
    try:
        db = get_db_manager()
        settings = db.load_notification_config()
        
        # 如果没有设置，返回默认配置
        if not settings:
            settings = {
                "chrome": {
                    "enabled": True,
                    "config": {},
                    "events": ["market", "strategy", "order", "risk"]
                },
                "telegram": {
                    "enabled": False,
                    "config": {
                        "bot_token": "",
                        "chat_id": ""
                    },
                    "events": ["market", "strategy", "order", "risk"]
                },
                "trading_reminder": {
                    "enabled": False,
                    "start_trading_time": "09:30",
                    "end_trading_time": "16:00",
                    "browser_notification_enabled": True,
                    "telegram_reminder_enabled": False
                }
            }
        
        return {"success": True, "settings": settings}
    except Exception as e:
        logger.error(f"Get notification settings error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/notifications/settings")
async def update_notification_settings(settings: Dict[str, Any], user: Optional[str] = Depends(get_current_user)):
    """更新通知设置"""
    try:
        db = get_db_manager()
        db.save_notification_config(settings)
        
        # 更新Telegram通知器配置
        if "telegram" in settings and telegram_notifier:
            telegram_config = settings["telegram"]
            if telegram_config.get("enabled") and telegram_config.get("config"):
                config = telegram_config["config"]
                if config.get("bot_token") and config.get("chat_id"):
                    telegram_notifier.bot_token = config["bot_token"]
                    telegram_notifier.chat_id = config["chat_id"]
        
        return {"success": True, "message": "Notification settings updated"}
    except Exception as e:
        logger.error(f"Update notification settings error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/notifications/history")
async def get_notification_history(
    channel: Optional[str] = None,
    status: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 100,
    user: Optional[str] = Depends(get_current_user)
):
    """获取通知历史"""
    try:
        db = get_db_manager()
        history = db.load_notification_history(
            channel=channel,
            status=status,
            since=since,
            limit=limit
        )
        
        return {"success": True, "history": history}
    except Exception as e:
        logger.error(f"Get notification history error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/notifications/test")
async def test_notification(
    req: NotificationTestRequest,
    user: Optional[str] = Depends(get_current_user)
):
    """测试通知发送"""
    try:
        # 使用统一的NotificationManager发送测试通知
        success = await notification_manager.send(
            message=req.message,
            event_type="test",
            category="api",
            channel=req.channel
        )
        
        status = "sent" if success else "failed"
        error_msg = None if success else f"Failed to send {req.channel} notification"
        
        return {
            "success": status == "sent",
            "message": "Test notification sent" if status == "sent" else error_msg
        }
    except Exception as e:
        logger.error(f"Test notification error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# System Logs API (kept for general debugging)
@app.get("/api/logs")
async def get_logs(
    lines: int = 50,  # 减少默认行数
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
        
        # 使用异步文件读取，避免阻塞
        import aiofiles
        async with aiofiles.open(log_file, "r") as f:
            content = await f.readlines()
            # 限制最大读取行数，避免内存问题
            if len(content) > 10000:
                content = content[-10000:]

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
            # Inclusive level filtering: warning -> WARNING, ERROR, CRITICAL
            levels_order = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
            try:
                idx = levels_order.index(level.upper())
                allowed = set(levels_order[idx:])
            except ValueError:
                allowed = set(levels_order)  # fallback to all
            def extract_level(line: str):
                try:
                    parts = line.split('|')
                    # Expected format: | YYYY-mm-dd HH:MM:SS | LEVEL | MODULE | ...
                    if len(parts) >= 3:
                        return parts[2].strip().upper()
                except Exception:
                    return None
                return None
            content = [line for line in content if extract_level(line) in allowed]

        if module != "all":
            content = [line for line in content if f"| {module} |" in line]

        if request_id:
            content = [line for line in content if request_id in line]

        return {"success": True, "logs": content[-lines:]}
    except Exception as e:
        logger.error(f"Get logs error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/logs/modules")
async def get_log_modules(
    range: str = "today",
    user: Optional[str] = Depends(get_current_user),
):
    """Get unique module names from logs for dropdown filtering."""
    try:
        log_file = "logs/trading_bot.log"
        if not os.path.exists(log_file):
            return {"success": True, "modules": []}
        
        import aiofiles
        async with aiofiles.open(log_file, "r") as f:
            content = await f.readlines()
            # 限制最大读取行数，避免内存问题
            if len(content) > 10000:
                content = content[-10000:]

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

        # Extract unique modules from log lines
        modules = set()
        for line in content:
            try:
                parts = line.split('|')
                # Expected format: | YYYY-mm-dd HH:MM:SS | LEVEL | MODULE | ...
                if len(parts) >= 4:
                    module = parts[3].strip()
                    if module and module != "-":
                        modules.add(module)
            except Exception:
                continue
        
        # Sort modules alphabetically
        sorted_modules = sorted(list(modules))
        
        return {"success": True, "modules": sorted_modules}
    except Exception as e:
        logger.error(f"Get log modules error: {e}")
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
