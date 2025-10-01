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
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import List, Optional, Dict, Any, Callable, Awaitable
from loguru import logger
from src.config import load_project_env
from dataclasses import asdict, dataclass, field
import uuid
import contextvars
import time
import re
import inspect
from collections import OrderedDict
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
from src.cache import hgetall
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
from src.routes.monitoring import router as monitoring_router
from src.routes.service import router as service_router
from src.routes.config import router as config_router
from src.routes.websocket import router as websocket_router
from src.routes.dom import router as dom_router
from src.data_layer import get_data_source_manager


# Global variables
load_project_env()


def get_core_services() -> core.CoreContainer:
    return core.require_container()


def _get_interval_env(name: str, default: int) -> int:
    """Read a non-negative interval (seconds) from environment variables.

    Falls back to ``default`` when the value is missing or invalid and clamps
    negative values to zero so the caller can treat ``0`` as "no throttling".
    """

    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(0, value)


# Redis health monitoring configuration for IB command service
IB_COMMAND_HEALTH_KEY = "hash:service:health:ib-command"
IB_COMMAND_HEALTH_POLL_INTERVAL_SEC = _get_interval_env(
    "IB_COMMAND_HEALTH_POLL_INTERVAL_SEC",
    5,
)
IB_COMMAND_HEALTH_STALE_AFTER_SEC = _get_interval_env(
    "IB_COMMAND_HEALTH_STALE_AFTER_SEC",
    15,
)

# Context variables for request-scoped logging
request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
module_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("module", default="-")


# Timezone helpers
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
NY_TZ = ZoneInfo("America/New_York")



def resolve_module(path: str) -> str:
    """Simple mapping from request path to module name."""

    if path.startswith("/api/strategies"):
        # All `/api/strategies` routes operate on internal strategies. The external
        # webhook uses `/api/strategy`, so treat the plural form as internal to avoid
        # logging confusion.
        return "internal_strategy"
    if path.startswith("/api/strategy"):
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

@dataclass
class ModuleHandle:
    """Runtime handle returned by module start functions."""

    name: str
    tasks: List[asyncio.Task[Any]] = field(default_factory=list)
    finalizers: List[Callable[[], Awaitable[None] | None]] = field(default_factory=list)


@dataclass
class ModuleDefinition:
    """Declarative definition of a pluggable module."""

    name: str
    start: Callable[["ModuleRuntimeContext"], Awaitable[ModuleHandle]]
    description: str = ""


@dataclass
class ModuleRuntimeContext:
    """Context passed to module start functions."""

    app: FastAPI
    module_name: str
    logger: Any
    root_logger: Any


class ModuleRegistry:
    """Registry orchestrating module startup and shutdown."""

    def __init__(self, root_logger):
        self._root_logger = root_logger
        self._definitions: "OrderedDict[str, ModuleDefinition]" = OrderedDict()
        self._handles: List[ModuleHandle] = []

    def register(self, definition: ModuleDefinition) -> None:
        self._definitions[definition.name] = definition

    def available_modules(self) -> List[str]:
        return list(self._definitions.keys())

    def resolve_enabled(self, raw: Optional[str]) -> List[str]:
        if raw is None or not raw.strip():
            return self.available_modules()

        tokens = [token.strip() for token in raw.split(",") if token.strip()]
        if not tokens:
            return []

        if any(token.upper() == "ALL" for token in tokens):
            return self.available_modules()

        unknown = [token for token in tokens if token not in self._definitions]
        if unknown:
            self._root_logger.warning(
                f"Unknown modules in ENABLED_MODULES: {', '.join(unknown)}"
            )

        deduped: "OrderedDict[str, None]" = OrderedDict()
        for token in tokens:
            if token in self._definitions:
                deduped.setdefault(token, None)
        return list(deduped.keys())

    async def start_modules(self, app: FastAPI, enabled: List[str]) -> None:
        for name in enabled:
            definition = self._definitions.get(name)
            if definition is None:
                continue

            module_logger = self._root_logger.bind(module=name, request_id="-")
            context = ModuleRuntimeContext(
                app=app,
                module_name=name,
                logger=module_logger,
                root_logger=self._root_logger,
            )
            try:
                handle = await definition.start(context)
            except Exception as exc:
                module_logger.exception(f"Module {name} failed to start: {exc}")
                handle = ModuleHandle(name=name)

            if handle.name != name:
                handle.name = name
            self._handles.append(handle)

    async def shutdown_modules(self) -> None:
        for handle in reversed(self._handles):
            for task in handle.tasks:
                if not task.done():
                    task.cancel()

        for handle in reversed(self._handles):
            for task in handle.tasks:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    self._root_logger.warning(
                        f"Background task shutdown warning ({handle.name}): {exc}"
                    )

        for handle in reversed(self._handles):
            for finalizer in handle.finalizers:
                try:
                    result = finalizer()
                    if inspect.isawaitable(result):
                        await result
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    self._root_logger.warning(
                        f"Module shutdown warning ({handle.name}): {exc}"
                    )

        self._handles.clear()


async def _module_data_subscriptions(context: ModuleRuntimeContext) -> ModuleHandle:
    handle = ModuleHandle(name=context.module_name)

    try:
        get_data_source_manager()
    except Exception as exc:
        context.logger.warning(f"Data subscription manager initialisation warning: {exc}")
    else:
        context.logger.info("Data source manager initialised for subscriptions")

    try:
        dom_service = core.get_dom_service()
    except Exception as exc:
        context.logger.warning(f"DOM service unavailable for auto-start: {exc}")
        return handle

    try:
        scheduled = dom_service.ensure_auto_start_worker()
    except Exception as exc:
        context.logger.warning(f"DOM auto-start scheduling warning: {exc}")
        return handle

    symbol = dom_service.auto_start_symbol
    if scheduled:
        if symbol:
            context.logger.info(
                "DOM auto-start worker scheduled for symbol {}",
                symbol,
            )
        else:
            context.logger.info("DOM auto-start worker scheduled (no default symbol configured)")
    else:
        if symbol:
            context.logger.info(
                "DOM auto-start worker already initialised for symbol {}",
                symbol,
            )
        else:
            context.logger.info("DOM auto-start disabled; no default symbol configured")

    return handle

async def _module_risk_manager(context: ModuleRuntimeContext) -> ModuleHandle:
    handle = ModuleHandle(name=context.module_name)

    try:
        services = get_core_services()
        services.risk_manager.start()
        context.logger.info("Risk manager started during application startup")
    except Exception as exc:
        context.logger.warning(f"Risk manager startup warning: {exc}")
        return handle

    def _shutdown_risk_manager() -> None:
        try:
            services = get_core_services()
            manager = services.risk_manager
            if getattr(manager, 'scheduler', None) and manager.scheduler.running:
                manager.scheduler.shutdown()
                context.logger.info("Risk manager scheduler shutdown")
        except Exception as exc:
            context.logger.warning(f"Risk manager shutdown warning: {exc}")

    handle.finalizers.append(_shutdown_risk_manager)
    return handle


async def _module_account_snapshot(context: ModuleRuntimeContext) -> ModuleHandle:
    handle = ModuleHandle(name=context.module_name)

    try:
        services = get_core_services()
        services.account_snapshot_service.start()
        context.logger.info("Account snapshot service started during application startup")
    except Exception as exc:
        context.logger.warning(
            f"Account snapshot service startup warning: {exc}"
        )
        return handle

    async def _stop_account_snapshot() -> None:
        try:
            services = get_core_services()
            await services.account_snapshot_service.stop()
            context.logger.info("Account snapshot service stopped")
        except Exception as exc:
            context.logger.warning(
                f"Account snapshot service shutdown warning: {exc}"
            )

    handle.finalizers.append(_stop_account_snapshot)
    return handle


async def _module_strategy_manager(context: ModuleRuntimeContext) -> ModuleHandle:
    handle = ModuleHandle(name=context.module_name)

    try:
        services = get_core_services()
        strategy_manager = services.strategy_manager
        strategy_manager.start()
        context.logger.info("Strategy manager initialized during application startup")
    except Exception as exc:
        context.logger.warning(f"Strategy manager startup warning: {exc}")
        return handle

    async def _restore_data_push_states() -> None:
        try:
            services = get_core_services()
            await services.strategy_manager.restore_data_push_states()
            context.logger.info("Strategy data push states restored after startup")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            context.logger.warning(
                f"Deferred data push restoration failed: {exc}"
            )

    handle.tasks.append(
        asyncio.create_task(
            _restore_data_push_states(),
            name="module-strategy-restore",
        )
    )

    def _stop_strategy_manager() -> None:
        try:
            services = get_core_services()
            services.strategy_manager.stop()
            context.logger.info("Strategy manager scheduler stopped")
        except Exception as exc:
            context.logger.warning(f"Strategy manager shutdown warning: {exc}")

    handle.finalizers.append(_stop_strategy_manager)
    return handle


async def _module_queue_worker(context: ModuleRuntimeContext) -> ModuleHandle:
    handle = ModuleHandle(name=context.module_name)
    services = get_core_services()

    try:
        await services.queue_manager.start_worker()
        context.logger.info("Queue worker started during application startup")
    except Exception as exc:
        context.logger.warning(f"Queue worker startup warning: {exc}")
        return handle

    async def _stop_queue_worker() -> None:
        try:
            services = get_core_services()
            await services.queue_manager.stop_worker()
            context.logger.info("Queue worker stopped")
        except Exception as exc:
            context.logger.warning(f"Queue worker shutdown warning: {exc}")

    handle.finalizers.append(_stop_queue_worker)
    return handle


def _resolve_ib_account_type() -> str:
    """Resolve the account type label for IB command service health."""

    override = os.getenv("IB_COMMAND_ACCOUNT_TYPE") or os.getenv("IB_ACCOUNT_TYPE")
    if override:
        cleaned = override.strip()
        if cleaned:
            return cleaned.title()

    flag = os.getenv("IBKR_PAPER_TRADING", "true").strip().lower()
    if flag in {"0", "false", "no", "live"}:
        return "Live"
    return "Paper"


def _build_ib_command_health_payload(
    raw: Dict[str, str], *, stale_after: float
) -> Dict[str, Any]:
    """Normalise Redis health fields for websocket broadcasting."""

    now = datetime.now(timezone.utc)
    last_seen_raw = raw.get("last_seen", "")
    last_seen_dt: datetime | None = None
    age_seconds: float | None = None

    if last_seen_raw:
        try:
            last_seen_dt = datetime.fromisoformat(last_seen_raw)
        except ValueError:
            last_seen_dt = None
        else:
            delta = now - last_seen_dt
            age_seconds = max(delta.total_seconds(), 0.0)

    threshold = stale_after if stale_after > 0 else 30.0
    stale = age_seconds is None or age_seconds > threshold

    queue_depth_raw = raw.get("queue_depth")
    queue_depth: int | None = None
    if queue_depth_raw is not None:
        try:
            queue_depth = int(queue_depth_raw)
        except (TypeError, ValueError):
            queue_depth = None

    last_error_raw = raw.get("last_error")
    last_error = last_error_raw.strip() if last_error_raw else ""
    if not last_error:
        last_error = None

    connected = (
        not stale
        and last_error is None
        and queue_depth is not None
        and queue_depth >= 0
    )

    return {
        "type": "connection_status",
        "connected": connected,
        "account_type": _resolve_ib_account_type(),
        "last_seen": last_seen_dt.isoformat() if last_seen_dt else None,
        "queue_depth": queue_depth,
        "last_error": last_error,
        "stale": stale,
        "source": "redis",
    }


async def _fetch_ib_command_health_payload(
    *, stale_after: float, logger_: Any
) -> Dict[str, Any]:
    try:
        raw = await hgetall(IB_COMMAND_HEALTH_KEY)
    except Exception as exc:  # pragma: no cover - defensive guard
        logger_.debug("Failed to fetch IB command health from Redis: {}", exc)
        raw = {}
    return _build_ib_command_health_payload(raw, stale_after=stale_after)


async def _module_service_health_monitor(
    context: ModuleRuntimeContext,
) -> ModuleHandle:
    handle = ModuleHandle(name=context.module_name)

    interval = IB_COMMAND_HEALTH_POLL_INTERVAL_SEC
    if interval <= 0:
        context.logger.info(
            "IB command health monitor disabled (poll interval <= 0)."
        )
        return handle

    stale_after = IB_COMMAND_HEALTH_STALE_AFTER_SEC
    if stale_after <= 0:
        stale_after = max(interval * 3, 30)

    services = get_core_services()
    ws_manager = services.websocket_manager

    async def _run_monitor() -> None:
        last_payload: Dict[str, Any] | None = None
        try:
            while True:
                payload = await _fetch_ib_command_health_payload(
                    stale_after=stale_after,
                    logger_=context.logger,
                )
                if ws_manager.connections:
                    if last_payload != payload:
                        await ws_manager.broadcast(payload)
                        last_payload = dict(payload)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            context.logger.debug("IB command health monitor cancelled")
        except Exception as exc:
            context.logger.warning(
                "IB command health monitor encountered an error: {}",
                exc,
            )

    task = asyncio.create_task(
        _run_monitor(),
        name="module-service-health-monitor",
    )
    handle.tasks.append(task)
    return handle


def _build_module_registry(main_logger) -> ModuleRegistry:
    registry = ModuleRegistry(main_logger)
    registry.register(
        ModuleDefinition(
            name="data_subscriptions",
            start=_module_data_subscriptions,
            description="Initialise unified data subscriptions and DOM auto-start.",
        )
    )
    registry.register(
        ModuleDefinition(
            name="risk_manager",
            start=_module_risk_manager,
            description="Start the risk manager scheduler.",
        )
    )
    registry.register(
        ModuleDefinition(
            name="account_snapshot",
            start=_module_account_snapshot,
            description="Run the periodic account snapshot service.",
        )
    )
    registry.register(
        ModuleDefinition(
            name="strategy_manager",
            start=_module_strategy_manager,
            description="Boot the strategy manager and restore push states.",
        )
    )
    registry.register(
        ModuleDefinition(
            name="queue_worker",
            start=_module_queue_worker,
            description="Start the background queue worker for internal orders.",
        )
    )
    registry.register(
        ModuleDefinition(
            name="service_health_monitor",
            start=_module_service_health_monitor,
            description="Broadcast IB command service health to connected clients.",
        )
    )
    return registry


@asynccontextmanager
async def lifespan(app: FastAPI):
    main_logger = get_module_logger("main")
    registry = _build_module_registry(main_logger)

    services = await core.initialise()
    app.state.core = services

    try:
        await services.run_health_checks()
    except Exception as exc:
        main_logger.warning("Startup health checks failed: {}", exc)

    enabled_raw = os.getenv("ENABLED_MODULES", "ALL")
    enabled_modules = registry.resolve_enabled(enabled_raw)

    if enabled_modules:
        main_logger.info(
            "Starting modules: {}", ", ".join(enabled_modules)
        )
    else:
        main_logger.warning(
            "No modules enabled; the application will run without background workers."
        )

    await registry.start_modules(app, enabled_modules)

    try:
        yield
    finally:
        await registry.shutdown_modules()
        app.state.core = None
        await core.shutdown()
        main_logger.info("Application shutdown complete.")


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

def _build_cors_origins() -> List[str]:
    """Construct the list of allowed CORS origins.

    The web UI and API are typically served from the same FastAPI process but
    the exposed port is configurable through ``WEB_PORT``.  When the port is
    changed (for example to 8005 as used in several deployment guides) the
    hard-coded 8000 origins prevented browsers from calling the API because the
    ``Origin`` header no longer matched the allow list.  Building the origin
    list dynamically keeps local development behaviour intact while supporting
    custom ports or hosts.
    """

    configured = os.getenv("CORS_ALLOW_ORIGINS")
    if configured:
        origins = [origin.strip() for origin in configured.split(",") if origin.strip()]
        return origins or ["*"]

    web_port = os.getenv("WEB_PORT", "8000").strip() or "8000"
    web_host = os.getenv("WEB_HOST", "0.0.0.0").strip() or "0.0.0.0"

    # Default localhost candidates plus the explicit host if it's not a wildcard.
    host_candidates = ["localhost", "127.0.0.1"]
    if web_host not in {"0.0.0.0", "::"}:
        host_candidates.append(web_host)

    origins: List[str] = []
    for host in dict.fromkeys(host_candidates):  # Preserve order while deduplicating
        origins.append(f"http://{host}:{web_port}")
        origins.append(f"https://{host}:{web_port}")

    return origins


app.add_middleware(
    CORSMiddleware,
    allow_origins=_build_cors_origins(),
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
app.include_router(monitoring_router)
app.include_router(service_router)
app.include_router(config_router)
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
            host=web_host,
            port=web_port,
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
