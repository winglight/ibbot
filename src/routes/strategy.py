"""HTTP proxy routes for the strategy microservice."""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Dict, Mapping, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from loguru import logger

import core
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
STRATEGY_CONFIG_HASH_KEY = os.getenv("STRATEGY_CONFIG_HASH_KEY", "hash:config:strategy")
STRATEGY_CONFIG_MODULE = "strategy"

_BOOLEAN_FIELDS = {"enable_auth", "pause_data_push_on_holidays"}
_JSON_FIELDS = {"api_payload_template", "strategies"}

_TRUE_TOKENS = {"1", "true", "yes", "on", "enabled"}
_FALSE_TOKENS = {"0", "false", "no", "off", "disabled"}

_STRATEGY_SERVICE_CLIENT = StrategyServiceClient()


def _get_strategy_service_client() -> StrategyServiceClient:
    return _STRATEGY_SERVICE_CLIENT


def _normalise_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="ignore")
    text = str(value).strip()
    return text or None


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = _normalise_text(value)
    if text is None:
        return None
    lowered = text.lower()
    if lowered in _TRUE_TOKENS:
        return True
    if lowered in _FALSE_TOKENS:
        return False
    return None


def _parse_json_value(value: Any) -> Any | None:
    if isinstance(value, (dict, list)):
        return value
    text = _normalise_text(value)
    if text is None:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):  # pragma: no cover - defensive guard
        logger.debug("Failed to parse cached strategy JSON field", value=text)
        return None


def _deserialise_cached_settings(values: Mapping[str, Any]) -> Dict[str, Any]:
    settings: Dict[str, Any] = {}
    for key, raw_value in values.items():
        if key in _BOOLEAN_FIELDS:
            coerced = _coerce_bool(raw_value)
            if coerced is not None:
                settings[key] = coerced
            continue
        if key in _JSON_FIELDS:
            parsed = _parse_json_value(raw_value)
            if parsed is not None:
                settings[key] = parsed
            continue
        normalised = _normalise_text(raw_value)
        if normalised is not None:
            settings[key] = normalised
    return settings


async def _load_cached_strategy_settings() -> Dict[str, Any] | None:
    container = core.get_container()
    provider = getattr(container, "config_provider", None) if container else None
    if provider is not None:
        try:
            values = await provider.get_module(STRATEGY_CONFIG_MODULE)
        except Exception as exc:  # pragma: no cover - config lookup failures depend on runtime
            logger.debug(
                "Failed to load cached strategy settings from config provider",
                error=str(exc),
            )
        else:
            if values:
                return _deserialise_cached_settings(values)
            return {}

    try:
        redis = await get_redis_client()
    except Exception as exc:  # pragma: no cover - redis initialisation failures are runtime specific
        logger.debug("Failed to acquire Redis client for cached strategy settings", error=str(exc))
        return None

    try:
        values = await redis.hgetall(STRATEGY_CONFIG_HASH_KEY)
    except Exception as exc:  # pragma: no cover - redis access failures depend on runtime state
        logger.debug(
            "Failed to load cached strategy settings from Redis",
            key=STRATEGY_CONFIG_HASH_KEY,
            error=str(exc),
        )
        return None

    if not values:
        return {}

    return _deserialise_cached_settings(values)


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


async def _call_strategy_service(
    client: StrategyServiceClient,
    method: str,
    path: str,
    **kwargs: Any,
) -> Any:
    try:
        result = await client.request(method, path, **kwargs)
    except ServiceClientError as exc:
        _raise_strategy_service_error(exc)
    return result if result is not None else {}


@router.get("/api/strategy/settings")
async def get_strategy_settings(
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    try:
        result = await client.request("GET", "/strategy/settings")
    except ServiceUnavailableError as exc:
        cached = await _load_cached_strategy_settings()
        if cached is not None:
            logger.warning(
                "Strategy service unavailable; returning cached strategy settings",
                from_cache=True,
            )
            return {"settings": cached, "from_cache": True}
        raise HTTPException(status_code=503, detail="Strategy service unavailable") from exc
    except ServiceClientError as exc:
        _raise_strategy_service_error(exc)
    return result if result is not None else {}


@router.post("/api/strategy/settings")
async def update_strategy_settings(
    settings: StrategySettingsRequest,
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_service(
        client,
        "POST",
        "/strategy/settings",
        json=settings.model_dump(exclude_none=True),
    )


@router.post("/api/strategy/settings/generate-token")
async def generate_strategy_token(
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_service(
        client,
        "POST",
        "/strategy/settings/generate-token",
    )


@router.get("/api/strategy/files")
async def list_strategy_files(
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_service(client, "GET", "/strategy/files")


@router.get("/api/strategies")
async def list_strategies(
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_service(client, "GET", "/strategies")


@router.post("/api/strategies")
async def add_strategy_api(
    strategy: StrategyRequest,
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_service(
        client,
        "POST",
        "/strategies",
        json=strategy.model_dump(exclude_none=True),
    )


@router.get("/api/strategies/{name}")
async def get_strategy_api(
    name: str,
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_service(client, "GET", f"/strategies/{name}")


@router.get("/api/strategies/{name}/internal-info")
async def get_internal_strategy_info_api(
    name: str,
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_service(
        client,
        "GET",
        f"/strategies/{name}/internal-info",
    )


@router.post("/api/strategies/{name}/internal-info/parameter")
async def update_strategy_parameter_api(
    name: str,
    payload: StrategyParameterUpdateRequest,
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_service(
        client,
        "POST",
        f"/strategies/{name}/internal-info/parameter",
        json=payload.model_dump(exclude_none=True),
    )


@router.get("/api/strategies/{name}/risk-settings")
async def get_strategy_risk_settings_api(
    name: str,
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_service(
        client,
        "GET",
        f"/strategies/{name}/risk-settings",
    )


@router.put("/api/strategies/{name}/risk-settings")
async def update_strategy_risk_settings_api(
    name: str,
    payload: StrategyRiskSettings,
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_service(
        client,
        "PUT",
        f"/strategies/{name}/risk-settings",
        json=payload.model_dump(exclude_unset=True),
    )


@router.get("/api/strategies/{name}/status")
async def get_internal_strategy_status_api(
    name: str,
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_service(
        client,
        "GET",
        f"/strategies/{name}/status",
    )


@router.post("/api/strategies/{name}/data-push")
async def request_strategy_data_push(
    name: str,
    payload: StrategyDataPushRequest,
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_service(
        client,
        "POST",
        f"/strategies/{name}/data-push",
        json=payload.model_dump(exclude_none=True),
    )


@router.put("/api/strategies/{name}")
async def update_strategy_api(
    name: str,
    updates: Dict[str, Any],
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_service(
        client,
        "PUT",
        f"/strategies/{name}",
        json=dict(updates),
    )


@router.post("/api/strategies/{name}/rename")
async def rename_strategy_api(
    name: str,
    payload: StrategyRenameRequest,
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_service(
        client,
        "POST",
        f"/strategies/{name}/rename",
        json=payload.model_dump(exclude_none=True),
    )


@router.delete("/api/strategies/{name}")
async def delete_strategy_api(
    name: str,
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_service(
        client,
        "DELETE",
        f"/strategies/{name}",
    )


@router.post("/api/strategies/{name}/toggle")
async def toggle_strategy_api(
    name: str,
    enabled: bool,
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_service(
        client,
        "POST",
        f"/strategies/{name}/toggle",
        json={"enabled": bool(enabled)},
    )


@router.post("/api/strategies/{name}/reset")
async def reset_strategy_data_api(
    name: str,
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_service(
        client,
        "POST",
        f"/strategies/{name}/reset",
    )


@router.get("/api/strategies/{name}/performance")
async def get_strategy_performance_api(
    name: str,
    range: str = "1d",
    start: Optional[str] = None,
    end: Optional[str] = None,
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    params = {"range": range}
    if start:
        params["start"] = start
    if end:
        params["end"] = end
    return await _call_strategy_service(
        client,
        "GET",
        f"/strategies/{name}/performance",
        params=params,
    )


@router.get("/api/strategies/{name}/pnl/daily")
async def get_strategy_daily_pnl(
    name: str,
    force_refresh: bool = False,
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    params = {"force_refresh": str(force_refresh).lower()}
    return await _call_strategy_service(
        client,
        "GET",
        f"/strategies/{name}/pnl/daily",
        params=params,
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
        return await client.request("POST", "/strategy", json=data)
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
    params = {
        "limit": max(1, min(int(limit or 1), 1000)),
        "force_refresh": str(force_refresh).lower(),
    }
    return await _call_strategy_service(
        client,
        "GET",
        f"/strategies/{name}/executions",
        params=params,
    )


@router.delete("/api/strategies/{name}/executions")
async def clear_strategy_executions(
    name: str,
    user: Optional[str] = Depends(get_current_user),
    client: StrategyServiceClient = Depends(_get_strategy_service_client),
) -> Any:
    return await _call_strategy_service(
        client,
        "DELETE",
        f"/strategies/{name}/executions",
    )
