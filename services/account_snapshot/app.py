"""FastAPI application for the account snapshot service."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

from .runtime import AccountSnapshotRuntime, RuntimeStatus


class RefreshRequest(BaseModel):
    reason: Optional[str] = None
    fast_positions: Optional[bool] = None


class MarkStaleRequest(BaseModel):
    reason: Optional[str] = None


def runtime_dependency(request: Request) -> AccountSnapshotRuntime:
    runtime = getattr(request.app.state, "account_snapshot_runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="Account snapshot runtime unavailable")
    return runtime


def create_app(runtime: Optional[AccountSnapshotRuntime] = None) -> FastAPI:
    runtime = runtime or AccountSnapshotRuntime()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await runtime.startup()
        app.state.account_snapshot_runtime = runtime
        try:
            yield
        finally:
            await runtime.shutdown()
            if hasattr(app.state, "account_snapshot_runtime"):
                delattr(app.state, "account_snapshot_runtime")

    app = FastAPI(lifespan=lifespan)

    @app.get("/health")
    async def health(runtime: AccountSnapshotRuntime = Depends(runtime_dependency)) -> dict:
        status: RuntimeStatus = runtime.status()
        return {
            "status": "ok",
            "runtime": status.to_dict(),
        }

    @app.post("/refresh")
    async def trigger_refresh(
        request: RefreshRequest,
        runtime: AccountSnapshotRuntime = Depends(runtime_dependency),
    ) -> dict:
        result = await runtime.refresh_both(
            reason=request.reason or "api",
            fast_positions=request.fast_positions if request.fast_positions is not None else True,
        )
        return {"success": True, "result": result}

    @app.post("/refresh/account-info")
    async def refresh_account_info(
        request: RefreshRequest,
        runtime: AccountSnapshotRuntime = Depends(runtime_dependency),
    ) -> dict:
        data = await runtime.refresh_account_info(reason=request.reason or "api")
        return {"success": data is not None, "account_info": data}

    @app.post("/refresh/positions")
    async def refresh_positions(
        request: RefreshRequest,
        runtime: AccountSnapshotRuntime = Depends(runtime_dependency),
    ) -> dict:
        data = await runtime.refresh_positions(
            reason=request.reason or "api",
            fast=request.fast_positions if request.fast_positions is not None else True,
        )
        return {"success": data is not None, "positions": data}

    @app.post("/mark-stale")
    async def mark_stale(
        request: MarkStaleRequest,
        runtime: AccountSnapshotRuntime = Depends(runtime_dependency),
    ) -> dict:
        reason = request.reason or "api"
        await runtime.mark_stale(reason)
        return {"success": True, "reason": reason}

    return app


__all__ = ["create_app", "runtime_dependency"]
