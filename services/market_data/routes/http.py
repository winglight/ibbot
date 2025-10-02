"""HTTP routes exposing market data runtime state."""

from __future__ import annotations

from typing import Any, NoReturn

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

    try:
        snapshot = runtime.get_snapshot(symbol)
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - exercised via tests
        _raise_runtime_error(exc)

    if snapshot is None:
        raise HTTPException(status_code=404, detail="Symbol not subscribed")
    return snapshot


@router.get("/market")
async def list_market_snapshots(
    stale: bool | None = Query(None, description="Filter snapshots by stale flag"),
    runtime: MarketDataRuntime = Depends(_ensure_runtime),
) -> list[dict[str, Any]]:
    """Return the current runtime snapshots."""

    try:
        return runtime.get_snapshots(stale=stale)
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - exercised via tests
        _raise_runtime_error(exc)


@router.get("/health")
async def health_check(
    runtime: MarketDataRuntime = Depends(_ensure_runtime),
) -> dict[str, Any]:
    """Return health information for the market data runtime."""

    try:
        return await runtime.get_health_snapshot()
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - exercised via tests
        _raise_runtime_error(exc)


@router.get("/api/realtime_data/{symbol}")
async def legacy_realtime_data(
    symbol: str,
    runtime: MarketDataRuntime = Depends(_ensure_runtime),
) -> Any:
    """Backward compatible alias for the legacy realtime data endpoint."""

    return await get_market_snapshot(symbol, runtime)


__all__ = ["router"]


def _raise_runtime_error(exc: Exception) -> NoReturn:
    if isinstance(exc, HTTPException):
        raise exc
    if isinstance(exc, ConnectionError):
        detail = str(exc) or "Upstream data source unavailable"
        raise HTTPException(status_code=502, detail=detail) from exc
    detail = str(exc) or "Service unavailable"
    raise HTTPException(status_code=503, detail=detail) from exc
