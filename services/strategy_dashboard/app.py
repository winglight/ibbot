"""FastAPI application factory for the strategy dashboard service."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from .runtime import StrategyDashboardRuntime


def runtime_dependency(request: Request) -> StrategyDashboardRuntime:
    runtime = getattr(request.app.state, "strategy_dashboard_runtime", None)
    if runtime is None:
        raise RuntimeError("Strategy dashboard runtime unavailable")
    return runtime


def create_app(runtime: StrategyDashboardRuntime | None = None) -> FastAPI:
    runtime = runtime or StrategyDashboardRuntime()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await runtime.startup()
        app.state.strategy_dashboard_runtime = runtime
        try:
            yield
        finally:
            await runtime.shutdown()
            if hasattr(app.state, "strategy_dashboard_runtime"):
                delattr(app.state, "strategy_dashboard_runtime")

    app = FastAPI(lifespan=lifespan)

    from .routes.http import router as http_router

    app.include_router(http_router)

    return app


__all__ = ["create_app", "runtime_dependency"]
