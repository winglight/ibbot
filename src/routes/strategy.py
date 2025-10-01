"""HTTP proxy routes for the strategy microservice."""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger

from .auth import get_current_user
from src.models import (
    ExternalStrategyOrderRequest,
    StrategyDataPushRequest,
    StrategyParameterUpdateRequest,
    StrategyRenameRequest,
    StrategyRequest,
    StrategyRiskSettings,
    StrategySettingsRequest,
)

router = APIRouter()

STRATEGY_SERVICE_URL = os.getenv("STRATEGY_SERVICE_URL", "http://localhost:8045").rstrip("/")
STRATEGY_SERVICE_TIMEOUT = float(os.getenv("STRATEGY_SERVICE_TIMEOUT", "10.0"))


def _strategy_service_base_url() -> str:
    return STRATEGY_SERVICE_URL or "http://localhost:8045"


def _api_path(path: str) -> str:
    return path[4:] if path.startswith("/api") else path


def _extract_error_detail(response: httpx.Response) -> Any:
    try:
        payload = response.json()
    except ValueError:
        text = response.text.strip()
        return text or "Strategy service error"
    if isinstance(payload, dict):
        for key in ("detail", "message", "error", "reason"):
            value = payload.get(key)
            if value:
                return value
    return payload


async def _strategy_service_request(
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    json: Optional[Dict[str, Any]] = None,
) -> Any:
    try:
        async with httpx.AsyncClient(
            base_url=_strategy_service_base_url(),
            timeout=STRATEGY_SERVICE_TIMEOUT,
        ) as client:
            response = await client.request(method, _api_path(path), params=params, json=json)
    except httpx.HTTPError as exc:
        logger.error(
            "Strategy service request failed", method=method, path=path, error=str(exc)
        )
        raise HTTPException(status_code=503, detail="Strategy service unavailable") from exc

    if response.status_code >= 400:
        detail = _extract_error_detail(response)
        raise HTTPException(status_code=response.status_code, detail=detail)

    if not response.content:
        return {}

    try:
        return response.json()
    except ValueError as exc:  # pragma: no cover - defensive guard
        logger.error(
            "Strategy service returned non-JSON payload", status=response.status_code
        )
        raise HTTPException(status_code=502, detail="Invalid response from strategy service") from exc


@router.get("/api/strategy/settings")
async def get_strategy_settings(user: Optional[str] = Depends(get_current_user)) -> Any:
    return await _strategy_service_request("GET", "/strategy/settings")


@router.post("/api/strategy/settings")
async def update_strategy_settings(
    settings: StrategySettingsRequest,
    user: Optional[str] = Depends(get_current_user),
) -> Any:
    return await _strategy_service_request(
        "POST",
        "/strategy/settings",
        json=settings.model_dump(exclude_none=True),
    )


@router.post("/api/strategy/settings/generate-token")
async def generate_strategy_token(user: Optional[str] = Depends(get_current_user)) -> Any:
    return await _strategy_service_request("POST", "/strategy/settings/generate-token")


@router.get("/api/strategy/files")
async def list_strategy_files(user: Optional[str] = Depends(get_current_user)) -> Any:
    return await _strategy_service_request("GET", "/strategy/files")


@router.get("/api/strategies")
async def list_strategies(user: Optional[str] = Depends(get_current_user)) -> Any:
    return await _strategy_service_request("GET", "/strategies")


@router.post("/api/strategies")
async def add_strategy_api(
    strategy: StrategyRequest,
    user: Optional[str] = Depends(get_current_user),
) -> Any:
    return await _strategy_service_request(
        "POST",
        "/strategies",
        json=strategy.model_dump(exclude_none=True),
    )


@router.get("/api/strategies/{name}")
async def get_strategy_api(
    name: str,
    user: Optional[str] = Depends(get_current_user),
) -> Any:
    return await _strategy_service_request("GET", f"/strategies/{name}")


@router.get("/api/strategies/{name}/internal-info")
async def get_internal_strategy_info_api(
    name: str,
    user: Optional[str] = Depends(get_current_user),
) -> Any:
    return await _strategy_service_request("GET", f"/strategies/{name}/internal-info")


@router.post("/api/strategies/{name}/internal-info/parameter")
async def update_strategy_parameter(
    name: str,
    payload: StrategyParameterUpdateRequest,
    user: Optional[str] = Depends(get_current_user),
) -> Any:
    return await _strategy_service_request(
        "POST",
        f"/strategies/{name}/internal-info/parameter",
        json=payload.model_dump(exclude_none=True),
    )


@router.get("/api/strategies/{name}/risk-settings")
async def get_strategy_risk_settings_api(
    name: str,
    user: Optional[str] = Depends(get_current_user),
) -> Any:
    return await _strategy_service_request("GET", f"/strategies/{name}/risk-settings")


@router.put("/api/strategies/{name}/risk-settings")
async def update_strategy_risk_settings_api(
    name: str,
    payload: StrategyRiskSettings,
    user: Optional[str] = Depends(get_current_user),
) -> Any:
    return await _strategy_service_request(
        "PUT",
        f"/strategies/{name}/risk-settings",
        json=payload.model_dump(exclude_unset=True),
    )


@router.get("/api/strategies/{name}/status")
async def get_internal_strategy_status_api(
    name: str,
    user: Optional[str] = Depends(get_current_user),
) -> Any:
    return await _strategy_service_request("GET", f"/strategies/{name}/status")


@router.post("/api/strategies/{name}/data-push")
async def request_strategy_data_push(
    name: str,
    payload: StrategyDataPushRequest,
    user: Optional[str] = Depends(get_current_user),
) -> Any:
    return await _strategy_service_request(
        "POST",
        f"/strategies/{name}/data-push",
        json=payload.model_dump(exclude_none=True),
    )


@router.put("/api/strategies/{name}")
async def update_strategy_api(
    name: str,
    updates: Dict[str, Any],
    user: Optional[str] = Depends(get_current_user),
) -> Any:
    return await _strategy_service_request("PUT", f"/strategies/{name}", json=dict(updates))


@router.post("/api/strategies/{name}/rename")
async def rename_strategy_api(
    name: str,
    payload: StrategyRenameRequest,
    user: Optional[str] = Depends(get_current_user),
) -> Any:
    return await _strategy_service_request(
        "POST",
        f"/strategies/{name}/rename",
        json=payload.model_dump(exclude_none=True),
    )


@router.delete("/api/strategies/{name}")
async def delete_strategy_api(
    name: str,
    user: Optional[str] = Depends(get_current_user),
) -> Any:
    return await _strategy_service_request("DELETE", f"/strategies/{name}")


@router.post("/api/strategies/{name}/toggle")
async def toggle_strategy_api(
    name: str,
    enabled: bool,
    user: Optional[str] = Depends(get_current_user),
) -> Any:
    return await _strategy_service_request(
        "POST",
        f"/strategies/{name}/toggle",
        json={"enabled": bool(enabled)},
    )


@router.post("/api/strategies/{name}/reset")
async def reset_strategy_data_api(
    name: str,
    user: Optional[str] = Depends(get_current_user),
) -> Any:
    return await _strategy_service_request("POST", f"/strategies/{name}/reset")


@router.get("/api/strategies/{name}/performance")
async def get_strategy_performance_api(
    name: str,
    range: str = "1d",
    start: Optional[str] = None,
    end: Optional[str] = None,
    user: Optional[str] = Depends(get_current_user),
) -> Any:
    params = {"range": range}
    if start:
        params["start"] = start
    if end:
        params["end"] = end
    return await _strategy_service_request(
        "GET",
        f"/strategies/{name}/performance",
        params=params,
    )


@router.get("/api/strategies/{name}/pnl/daily")
async def get_strategy_daily_pnl(
    name: str,
    force_refresh: bool = False,
    user: Optional[str] = Depends(get_current_user),
) -> Any:
    params = {"force_refresh": str(force_refresh).lower()}
    return await _strategy_service_request(
        "GET",
        f"/strategies/{name}/pnl/daily",
        params=params,
    )


@router.post("/api/strategy")
async def submit_external_strategy_order(
    payload: ExternalStrategyOrderRequest,
    request: Request,
    user: Optional[str] = Depends(get_current_user),
) -> Any:
    data = payload.model_dump(exclude_none=True)
    client_host = request.client.host if request.client else None
    if client_host:
        metadata = data.setdefault("metadata", {})
        if isinstance(metadata, dict):
            metadata.setdefault("client", client_host)
    return await _strategy_service_request("POST", "/strategy", json=data)


@router.get("/api/strategies/{name}/executions")
async def get_strategy_executions(
    name: str,
    limit: int = 100,
    force_refresh: bool = False,
    user: Optional[str] = Depends(get_current_user),
) -> Any:
    params = {
        "limit": max(1, min(int(limit or 1), 1000)),
        "force_refresh": str(force_refresh).lower(),
    }
    return await _strategy_service_request(
        "GET",
        f"/strategies/{name}/executions",
        params=params,
    )


@router.delete("/api/strategies/{name}/executions")
async def clear_strategy_executions(
    name: str,
    user: Optional[str] = Depends(get_current_user),
) -> Any:
    return await _strategy_service_request("DELETE", f"/strategies/{name}/executions")
