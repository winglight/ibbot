"""FastAPI application wiring for the market data service."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket

from .runtime import MarketDataRuntime
from .websocket import MarketDataWebSocketManager


def runtime_dependency(request: Request) -> MarketDataRuntime:
    """Return the running :class:`MarketDataRuntime` instance for the request."""

    runtime = getattr(request.app.state, "market_data_runtime", None)
    if runtime is None:
        raise RuntimeError("Market data runtime is not initialised")
    return runtime


def create_app(runtime: MarketDataRuntime | None = None) -> FastAPI:
    """Create the FastAPI application bound to a :class:`MarketDataRuntime`."""

    runtime = runtime or MarketDataRuntime()
    ws_manager = MarketDataWebSocketManager(runtime)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await runtime.startup()
        await ws_manager.start()
        app.state.market_data_runtime = runtime
        app.state.market_data_ws_manager = ws_manager
        try:
            yield
        finally:
            await ws_manager.stop()
            await runtime.shutdown()
            if hasattr(app.state, "market_data_runtime"):
                delattr(app.state, "market_data_runtime")
            if hasattr(app.state, "market_data_ws_manager"):
                delattr(app.state, "market_data_ws_manager")

    app = FastAPI(lifespan=lifespan)

    from .routes.http import router as http_router

    app.include_router(http_router)

    @app.websocket("/ws/market")
    async def market_data_websocket(websocket: WebSocket) -> None:
        manager: MarketDataWebSocketManager = app.state.market_data_ws_manager
        query = websocket.query_params

        symbols_param = query.get("symbols")
        types_param = query.get("types") or query.get("event_types")

        symbols = _parse_filter_param(symbols_param)
        event_types = _parse_filter_param(types_param)

        await manager.handle_connection(
            websocket, symbols=symbols, event_types=event_types
        )

    return app


__all__ = ["create_app", "runtime_dependency"]


def _parse_filter_param(value: str | None) -> set[str] | None:
    if value is None:
        return None
    items = {item.strip() for item in value.split(",") if item.strip()}
    return items or None
