"""HTTP routes exposing market data runtime state."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ..app import runtime_dependency
from ..runtime import MarketDataRuntime

router = APIRouter()


def _ensure_runtime(runtime: MarketDataRuntime = Depends(runtime_dependency)) -> MarketDataRuntime:
    return runtime


@router.get("/market/{symbol}")
async def get_market_snapshot(
    symbol: str,
    runtime: MarketDataRuntime = Depends(_ensure_runtime),
) -> Any:
    """Return a runtime snapshot for a single symbol."""

    snapshot = runtime.get_snapshot(symbol)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Symbol not subscribed")
    return snapshot


@router.get("/market")
async def list_market_snapshots(
    stale: bool | None = Query(None, description="Filter snapshots by stale flag"),
    runtime: MarketDataRuntime = Depends(_ensure_runtime),
) -> list[dict[str, Any]]:
    """Return the current runtime snapshots."""

    return runtime.get_snapshots(stale=stale)


@router.get("/health")
async def health_check(
    runtime: MarketDataRuntime = Depends(_ensure_runtime),
) -> dict[str, Any]:
    """Return health information for the market data runtime."""

    return await runtime.get_health_snapshot()


@router.get("/api/realtime_data/{symbol}")
async def legacy_realtime_data(
    symbol: str,
    runtime: MarketDataRuntime = Depends(_ensure_runtime),
) -> Any:
    """Backward compatible alias for the legacy realtime data endpoint."""

    return await get_market_snapshot(symbol, runtime)


__all__ = ["router"]
