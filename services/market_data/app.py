"""FastAPI application wiring for the market data service."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from .runtime import MarketDataRuntime


def runtime_dependency(request: Request) -> MarketDataRuntime:
    """Return the running :class:`MarketDataRuntime` instance for the request."""

    runtime = getattr(request.app.state, "market_data_runtime", None)
    if runtime is None:
        raise RuntimeError("Market data runtime is not initialised")
    return runtime


def create_app(runtime: MarketDataRuntime | None = None) -> FastAPI:
    """Create the FastAPI application bound to a :class:`MarketDataRuntime`."""

    runtime = runtime or MarketDataRuntime()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await runtime.startup()
        app.state.market_data_runtime = runtime
        try:
            yield
        finally:
            await runtime.shutdown()
            if hasattr(app.state, "market_data_runtime"):
                delattr(app.state, "market_data_runtime")

    app = FastAPI(lifespan=lifespan)

    from .routes.http import router as http_router

    app.include_router(http_router)
    return app


__all__ = ["create_app", "runtime_dependency"]
