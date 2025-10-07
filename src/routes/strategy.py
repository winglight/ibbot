"""HTTP proxy routes for the strategy microservice."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
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
from src.routes.service_clients import (
    ServiceClientError,
    ServiceResponseError,
    ServiceUnavailableError,
    StrategyServiceClient,
)
from src.utils import now_local
from src.ibbot.redis import get_redis_client

router = APIRouter()

STRATEGY_QUEUE_KEY = os.getenv("STRATEGY_QUEUE_KEY", "list:queue:strategy")
STRATEGY_EVENT_STREAM_KEY = os.getenv("STRATEGY_EVENT_STREAM_KEY", "stream:strategy:events")

_STRATEGY_SERVICE_CLIENT = StrategyServiceClient()


def get_strategies_service_client() -> StrategyServiceClient:
    """Expose the shared strategy service client for legacy tests."""

    return _STRATEGY_SERVICE_CLIENT


def _get_strategy_service_client() -> StrategyServiceClient:
    return get_strategies_service_client()


def _raise_strategy_service_error(exc: ServiceClientError) -> None:
    if isinstance(exc, ServiceUnavailableError):
        raise HTTPException(status_code=503, detail="Strategy service unavailable") from exc
    if isinstance(exc, ServiceResponseError):
        detail = exc.detail or "Strategy service error"
        raise HTTPException(status_code=exc.status_code, detail=detail) from exc
    raise HTTPException(status_code=502, detail="Strategy service error") from exc


async def _enqueue_strategy_task(payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        redis = await get_redis_client()
    except Exception as exc:  # pragma: no cover - network failures surfaced in tests
        logger.error("Strategy queue unavailable", error=str(exc))
        raise HTTPException(status_code=503, detail="Strategy queue unavailable") from exc

    task_id = uuid.uuid4().hex
    queued_at = now_local().isoformat()
    command = {
        "task_id": task_id,
        "queued_at": queued_at,
        "strategy": payload.get("strategy"),
        "symbol": payload.get("symbol"),
        "action": payload.get("action"),
        "qty": payload.get("qty"),
        "type": payload.get("type"),
        "exchange": payload.get("exchange"),
        "sec_type": payload.get("sec_type"),
        "options": payload.get("options") or {},
        "metadata": payload.get("metadata") or {},
    }

    await redis.rpush(STRATEGY_QUEUE_KEY, json.dumps(command))

    status = "queued"
    event_payload: Dict[str, Any] | None = None
    try:
        entries = await redis.xrevrange(STRATEGY_EVENT_STREAM_KEY, count=1)
    except Exception as exc:  # pragma: no cover - best effort read
        logger.debug("Failed to read strategy event stream", error=str(exc))
        entries = []

    if entries:
        _, data = entries[0]
        if isinstance(data, dict):
            event_payload = dict(data)
            status = str(event_payload.get("status") or status)

    response: Dict[str, Any] = {
        "success": True,
        "task_id": task_id,
        "queue": STRATEGY_QUEUE_KEY,
        "status": status,
    }
    if event_payload:
        response["event"] = event_payload
    return response


StrategyClientCall = Callable[..., Awaitable[Any]]


async def _call_strategy_client(
    action: StrategyClientCall, *args: Any, **kwargs: Any
) -> Any:
    try:
        result = await action(*args, **kwargs)
    except ServiceClientError as exc:
        _raise_strategy_service_error(exc)
    return result if result is not None else {}


@router.get("/api/strategy/settings")
async def get_strategy_settings(
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_client(client.get_settings)


@router.post("/api/strategy/settings")
async def update_strategy_settings(
    settings: StrategySettingsRequest,
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_client(
        client.update_settings,
        settings.model_dump(exclude_none=True),
    )


@router.post("/api/strategy/settings/generate-token")
async def generate_strategy_token(
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_client(client.generate_settings_token)


@router.get("/api/strategy/files")
async def list_strategy_files(
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_client(client.list_files)


@router.get("/api/strategies")
async def list_strategies(
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_client(client.list_strategies)


@router.post("/api/strategies")
async def add_strategy_api(
    strategy: StrategyRequest,
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_client(
        client.create_strategy,
        strategy.model_dump(exclude_none=True),
    )


@router.get("/api/strategies/{name}")
async def get_strategy_api(
    name: str,
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_client(client.get_strategy, name)


@router.get("/api/strategies/{name}/internal-info")
async def get_internal_strategy_info_api(
    name: str,
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_client(client.get_internal_info, name)


@router.post("/api/strategies/{name}/internal-info/parameter")
async def update_strategy_parameter_api(
    name: str,
    payload: StrategyParameterUpdateRequest,
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_client(
        client.update_internal_parameter,
        name,
        payload.model_dump(exclude_none=True),
    )


@router.get("/api/strategies/{name}/risk-settings")
async def get_strategy_risk_settings_api(
    name: str,
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_client(client.get_risk_settings, name)


@router.put("/api/strategies/{name}/risk-settings")
async def update_strategy_risk_settings_api(
    name: str,
    payload: StrategyRiskSettings,
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_client(
        client.update_risk_settings,
        name,
        payload.model_dump(exclude_unset=True),
    )


@router.get("/api/strategies/{name}/status")
async def get_internal_strategy_status_api(
    name: str,
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_client(client.get_strategy_status, name)


@router.post("/api/strategies/{name}/data-push")
async def request_strategy_data_push(
    name: str,
    payload: StrategyDataPushRequest,
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_client(
        client.request_data_push,
        name,
        payload.model_dump(exclude_none=True),
    )


@router.put("/api/strategies/{name}")
async def update_strategy_api(
    name: str,
    updates: Dict[str, Any],
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_client(client.update_strategy, name, dict(updates))


@router.post("/api/strategies/{name}/rename")
async def rename_strategy_api(
    name: str,
    payload: StrategyRenameRequest,
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_client(
        client.rename_strategy,
        name,
        payload.model_dump(exclude_none=True),
    )


@router.delete("/api/strategies/{name}")
async def delete_strategy_api(
    name: str,
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_client(client.delete_strategy, name)


@router.post("/api/strategies/{name}/toggle")
async def toggle_strategy_api(
    name: str,
    enabled: bool,
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_client(
        client.toggle_strategy,
        name,
        enabled=bool(enabled),
    )


@router.post("/api/strategies/{name}/reset")
async def reset_strategy_data_api(
    name: str,
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_client(client.reset_strategy, name)


@router.get("/api/strategies/{name}/performance")
async def get_strategy_performance_api(
    name: str,
    range: str = "1d",
    start: Optional[str] = None,
    end: Optional[str] = None,
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_client(
        client.get_strategy_performance,
        name,
        range_=range,
        start=start,
        end=end,
    )


@router.get("/api/strategies/{name}/pnl/daily")
async def get_strategy_daily_pnl(
    name: str,
    force_refresh: bool = False,
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_client(
        client.get_daily_pnl,
        name,
        force_refresh=force_refresh,
    )


@router.post("/api/strategy")
async def submit_external_strategy_order(
    payload: ExternalStrategyOrderRequest,
    request: Request,
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    data = payload.model_dump(exclude_none=True)
    client_host = request.client.host if request.client else None
    if client_host:
        metadata = data.setdefault("metadata", {})
        if isinstance(metadata, dict):
            metadata.setdefault("client", client_host)

    try:
        return await client.submit_external_order(data)
    except ServiceUnavailableError as exc:
        logger.warning(
            "Strategy service unavailable; queuing external order",
            error=str(exc),
        )
        queued = await _enqueue_strategy_task(dict(data))
        queued["success"] = False
        queued["detail"] = "Strategy service unavailable; order queued"
        return JSONResponse(status_code=503, content=queued)
    except ServiceClientError as exc:
        _raise_strategy_service_error(exc)


@router.get("/api/strategies/{name}/executions")
async def get_strategy_executions(
    name: str,
    limit: int = 100,
    force_refresh: bool = False,
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_client(
        client.get_executions,
        name,
        limit=limit,
        force_refresh=force_refresh,
    )


@router.delete("/api/strategies/{name}/executions")
async def clear_strategy_executions(
    name: str,
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_client(client.clear_executions, name)
