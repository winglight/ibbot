"""HTTP routes for the strategy dashboard service."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ..app import runtime_dependency
from ..runtime import StrategyDashboardRuntime, compute_range
from src.models import ChartDataRequest

router = APIRouter()


def _runtime(
    runtime: StrategyDashboardRuntime = Depends(runtime_dependency),
) -> StrategyDashboardRuntime:
    return runtime


@router.get("/health")
async def health(runtime: StrategyDashboardRuntime = Depends(_runtime)) -> dict:
    return {"status": "ok"}


@router.get("/strategies/{name}/performance")
async def get_strategy_performance(
    name: str,
    range_param: str = Query("1d", alias="range"),
    start: Optional[str] = None,
    end: Optional[str] = None,
    runtime: StrategyDashboardRuntime = Depends(_runtime),
) -> dict:
    range_token, start_dt, end_dt = compute_range(range_param, start, end)
    payload = await runtime.get_strategy_performance(
        name,
        range_token=range_token,
        start=start_dt,
        end=end_dt,
    )
    return payload


@router.post("/chart_data")
async def chart_data(
    request: ChartDataRequest,
    runtime: StrategyDashboardRuntime = Depends(_runtime),
) -> dict:
    try:
        return await runtime.get_chart_data(request)
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - defensive logging
        raise HTTPException(status_code=500, detail=str(exc) or "Internal error") from exc


__all__ = ["router"]
