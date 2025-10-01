"""HTTP proxy routes for the strategy microservice."""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Sequence

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger

from core.dependencies import get_strategy_manager
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
from src.strategy_manager import StrategyManager
from src.utils import now_local, parse_local_datetime

router = APIRouter()

_strategy_service_url_env = os.getenv("STRATEGY_SERVICE_URL", "").strip()
STRATEGY_SERVICE_URL = _strategy_service_url_env.rstrip("/") if _strategy_service_url_env else None
DEFAULT_STRATEGY_SERVICE_URL = "http://localhost:8045"
STRATEGY_SERVICE_TIMEOUT = float(os.getenv("STRATEGY_SERVICE_TIMEOUT", "10.0"))


def _strategy_service_base_url() -> str:
    return STRATEGY_SERVICE_URL or DEFAULT_STRATEGY_SERVICE_URL


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


FallbackHandler = Callable[[], Awaitable[Any]]


async def _strategy_service_request(
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    json: Optional[Dict[str, Any]] = None,
    fallback: Optional[FallbackHandler] = None,
) -> Any:
    if not STRATEGY_SERVICE_URL:
        if fallback is not None:
            logger.info(
                "Strategy service URL not configured; using local manager", path=path
            )
            return await fallback()
        raise HTTPException(status_code=503, detail="Strategy service unavailable")

    try:
        async with httpx.AsyncClient(
            base_url=_strategy_service_base_url(),
            timeout=STRATEGY_SERVICE_TIMEOUT,
        ) as client:
            response = await client.request(
                method, _api_path(path), params=params, json=json
            )
    except httpx.HTTPError as exc:
        logger.error(
            "Strategy service request failed", method=method, path=path, error=str(exc)
        )
        if fallback is not None:
            logger.warning(
                "Strategy service call failed; using local manager fallback", path=path
            )
            return await fallback()
        raise HTTPException(status_code=503, detail="Strategy service unavailable") from exc

    if response.status_code >= 400:
        if fallback is not None:
            logger.warning(
                "Strategy service responded with error; using local manager fallback",
                path=path,
                status=response.status_code,
            )
            return await fallback()
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
        if fallback is not None:
            logger.warning(
                "Strategy service payload invalid; using local manager fallback", path=path
            )
            return await fallback()
        raise HTTPException(status_code=502, detail="Invalid response from strategy service") from exc


def _success_response(**payload: Any) -> Dict[str, Any]:
    response = {"success": True}
    response.update(payload)
    return response


def _list_strategy_files_from_manager(manager: StrategyManager) -> Sequence[str]:
    strategy_dir: Path = getattr(manager, "strategy_dir", Path("src/strategies"))
    files: Dict[str, None] = {}
    if strategy_dir.exists():
        for path in sorted(strategy_dir.rglob("*.py")):
            if path.name.startswith("__"):
                continue
            try:
                relative = path.relative_to(strategy_dir).as_posix()
            except ValueError:
                relative = path.as_posix()
            normaliser = getattr(manager, "_normalize_strategy_file_display", None)
            if callable(normaliser):
                display = normaliser(relative, path)
            else:
                display = relative
            if display:
                files[str(display)] = None
    return tuple(files.keys())


async def _get_strategy_settings_local(manager: StrategyManager) -> Dict[str, Any]:
    return _success_response(settings=manager.get_settings())


async def _update_strategy_settings_local(
    manager: StrategyManager, settings: StrategySettingsRequest
) -> Dict[str, Any]:
    updated = manager.update_settings(settings.model_dump(exclude_none=True))
    if not updated:
        raise HTTPException(status_code=500, detail="Failed to update strategy settings")
    return _success_response(settings=manager.get_settings())


async def _generate_strategy_token_local(manager: StrategyManager) -> Dict[str, Any]:
    token = manager.generate_auth_token()
    return _success_response(token=token)


async def _list_strategy_files_local(manager: StrategyManager) -> Dict[str, Any]:
    files = list(_list_strategy_files_from_manager(manager))
    return _success_response(files=files)


async def _list_strategies_local(manager: StrategyManager) -> Dict[str, Any]:
    return _success_response(strategies=manager.get_strategies())


async def _add_strategy_local(
    manager: StrategyManager, request: StrategyRequest
) -> Dict[str, Any]:
    payload = request.model_dump(exclude_none=True)
    if not manager.add_strategy(payload):
        raise HTTPException(status_code=400, detail="Failed to add strategy")
    return _success_response()


async def _get_strategy_local(manager: StrategyManager, name: str) -> Dict[str, Any]:
    strategy = manager.get_strategy(name)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return _success_response(strategy=strategy)


async def _get_internal_strategy_info_local(
    manager: StrategyManager, name: str
) -> Dict[str, Any]:
    info = manager.get_internal_strategy_info(name)
    if not info:
        raise HTTPException(status_code=404, detail="Strategy not found")
    cached = bool(info.get("stale"))
    return _success_response(strategy=info, cached=cached)


async def _update_strategy_parameter_local(
    manager: StrategyManager, name: str, payload: StrategyParameterUpdateRequest
) -> Dict[str, Any]:
    try:
        result = manager.update_strategy_parameter(name, payload.parameter_name, payload.value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _success_response(parameter=result)


async def _get_strategy_risk_settings_local(
    manager: StrategyManager, name: str
) -> Dict[str, Any]:
    settings = manager.get_strategy_risk_settings(name)
    if not settings:
        return _success_response(settings={})
    return _success_response(settings=settings)


async def _update_strategy_risk_settings_local(
    manager: StrategyManager, name: str, payload: StrategyRiskSettings
) -> Dict[str, Any]:
    try:
        settings = manager.update_strategy_risk_settings(
            name, payload.model_dump(exclude_unset=True)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _success_response(settings=settings)


async def _get_strategy_status_local(
    manager: StrategyManager, name: str
) -> Dict[str, Any]:
    status = manager.get_internal_strategy_status(name)
    if status is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return _success_response(status=status)


async def _set_strategy_data_push_local(
    manager: StrategyManager, name: str, payload: StrategyDataPushRequest
) -> Dict[str, Any]:
    try:
        result = await manager.set_data_push(
            name,
            bool(payload.enabled),
            subscription=payload.subscription,
            symbol=payload.symbol,
            force_restart=bool(payload.force_restart),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result if isinstance(result, dict) else _success_response()


async def _update_strategy_local(
    manager: StrategyManager, name: str, updates: Dict[str, Any]
) -> Dict[str, Any]:
    if not await manager.update_strategy(name, updates):
        raise HTTPException(status_code=400, detail="Failed to update strategy")
    strategy = manager.get_strategy(name)
    return _success_response(strategy=strategy) if strategy else _success_response()


async def _rename_strategy_local(
    manager: StrategyManager, name: str, payload: StrategyRenameRequest
) -> Dict[str, Any]:
    if not manager.rename_strategy(name, payload.new_name, bool(payload.rename_file)):
        raise HTTPException(status_code=400, detail="Failed to rename strategy")
    return _success_response(new_name=payload.new_name)


async def _delete_strategy_local(manager: StrategyManager, name: str) -> Dict[str, Any]:
    if not manager.delete_strategy(name):
        raise HTTPException(status_code=404, detail="Strategy not found")
    return _success_response()


async def _toggle_strategy_local(
    manager: StrategyManager, name: str, enabled: bool
) -> Dict[str, Any]:
    if not await manager.update_strategy(name, {"enabled": bool(enabled)}):
        raise HTTPException(status_code=400, detail="Failed to toggle strategy")
    return _success_response(enabled=bool(enabled))


async def _reset_strategy_local(manager: StrategyManager, name: str) -> Dict[str, Any]:
    try:
        result = manager.reset_strategy_data(name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _success_response(**result)


def _parse_range_value(value: Optional[str]) -> Optional[timedelta]:
    if not value:
        return None
    mapping = {
        "1d": timedelta(days=1),
        "3d": timedelta(days=3),
        "1w": timedelta(weeks=1),
        "2w": timedelta(weeks=2),
        "1m": timedelta(days=30),
        "3m": timedelta(days=90),
        "6m": timedelta(days=180),
        "1y": timedelta(days=365),
    }
    return mapping.get(value)


def _resolve_performance_dates(
    range_value: Optional[str], start: Optional[str], end: Optional[str]
) -> Dict[str, Any]:
    end_dt = parse_local_datetime(end) if end else None
    start_dt = parse_local_datetime(start) if start else None
    if end_dt is None:
        end_dt = now_local()
    if start_dt is None:
        delta = _parse_range_value(range_value)
        if delta is not None:
            start_dt = end_dt - delta
    payload: Dict[str, Any] = {"end_date": end_dt}
    if start_dt is not None:
        payload["start_date"] = start_dt
    return payload


async def _get_strategy_performance_local(
    manager: StrategyManager,
    name: str,
    range_value: Optional[str],
    start: Optional[str],
    end: Optional[str],
) -> Dict[str, Any]:
    kwargs = _resolve_performance_dates(range_value, start, end)
    metrics = manager.get_strategy_performance(name, **kwargs)
    return _success_response(metrics=metrics)


async def _get_strategy_executions_local(
    manager: StrategyManager, name: str, limit: int
) -> Dict[str, Any]:
    executions = manager.get_executions(name, limit)
    return _success_response(executions=executions)


async def _clear_strategy_executions_local(
    manager: StrategyManager, name: str
) -> Dict[str, Any]:
    deleted = manager.db_manager.delete_strategy_orders(name)
    return _success_response(deleted_orders=int(deleted or 0))


@router.get("/api/strategy/settings")
async def get_strategy_settings(
    user: Optional[str] = Depends(get_current_user),
    strategy_manager: StrategyManager = Depends(get_strategy_manager),
) -> Any:
    return await _strategy_service_request(
        "GET",
        "/strategy/settings",
        fallback=lambda: _get_strategy_settings_local(strategy_manager),
    )


@router.post("/api/strategy/settings")
async def update_strategy_settings(
    settings: StrategySettingsRequest,
    user: Optional[str] = Depends(get_current_user),
    strategy_manager: StrategyManager = Depends(get_strategy_manager),
) -> Any:
    return await _strategy_service_request(
        "POST",
        "/strategy/settings",
        json=settings.model_dump(exclude_none=True),
        fallback=lambda: _update_strategy_settings_local(strategy_manager, settings),
    )


@router.post("/api/strategy/settings/generate-token")
async def generate_strategy_token(
    user: Optional[str] = Depends(get_current_user),
    strategy_manager: StrategyManager = Depends(get_strategy_manager),
) -> Any:
    return await _strategy_service_request(
        "POST",
        "/strategy/settings/generate-token",
        fallback=lambda: _generate_strategy_token_local(strategy_manager),
    )


@router.get("/api/strategy/files")
async def list_strategy_files(
    user: Optional[str] = Depends(get_current_user),
    strategy_manager: StrategyManager = Depends(get_strategy_manager),
) -> Any:
    return await _strategy_service_request(
        "GET",
        "/strategy/files",
        fallback=lambda: _list_strategy_files_local(strategy_manager),
    )


@router.get("/api/strategies")
async def list_strategies(
    user: Optional[str] = Depends(get_current_user),
    strategy_manager: StrategyManager = Depends(get_strategy_manager),
) -> Any:
    return await _strategy_service_request(
        "GET",
        "/strategies",
        fallback=lambda: _list_strategies_local(strategy_manager),
    )


@router.post("/api/strategies")
async def add_strategy_api(
    strategy: StrategyRequest,
    user: Optional[str] = Depends(get_current_user),
    strategy_manager: StrategyManager = Depends(get_strategy_manager),
) -> Any:
    return await _strategy_service_request(
        "POST",
        "/strategies",
        json=strategy.model_dump(exclude_none=True),
        fallback=lambda: _add_strategy_local(strategy_manager, strategy),
    )


@router.get("/api/strategies/{name}")
async def get_strategy_api(
    name: str,
    user: Optional[str] = Depends(get_current_user),
    strategy_manager: StrategyManager = Depends(get_strategy_manager),
) -> Any:
    return await _strategy_service_request(
        "GET",
        f"/strategies/{name}",
        fallback=lambda: _get_strategy_local(strategy_manager, name),
    )


@router.get("/api/strategies/{name}/internal-info")
async def get_internal_strategy_info_api(
    name: str,
    user: Optional[str] = Depends(get_current_user),
    strategy_manager: StrategyManager = Depends(get_strategy_manager),
) -> Any:
    return await _strategy_service_request(
        "GET",
        f"/strategies/{name}/internal-info",
        fallback=lambda: _get_internal_strategy_info_local(strategy_manager, name),
    )


@router.post("/api/strategies/{name}/internal-info/parameter")
async def update_strategy_parameter(
    name: str,
    payload: StrategyParameterUpdateRequest,
    user: Optional[str] = Depends(get_current_user),
    strategy_manager: StrategyManager = Depends(get_strategy_manager),
) -> Any:
    return await _strategy_service_request(
        "POST",
        f"/strategies/{name}/internal-info/parameter",
        json=payload.model_dump(exclude_none=True),
        fallback=lambda: _update_strategy_parameter_local(strategy_manager, name, payload),
    )


@router.get("/api/strategies/{name}/risk-settings")
async def get_strategy_risk_settings_api(
    name: str,
    user: Optional[str] = Depends(get_current_user),
    strategy_manager: StrategyManager = Depends(get_strategy_manager),
) -> Any:
    return await _strategy_service_request(
        "GET",
        f"/strategies/{name}/risk-settings",
        fallback=lambda: _get_strategy_risk_settings_local(strategy_manager, name),
    )


@router.put("/api/strategies/{name}/risk-settings")
async def update_strategy_risk_settings_api(
    name: str,
    payload: StrategyRiskSettings,
    user: Optional[str] = Depends(get_current_user),
    strategy_manager: StrategyManager = Depends(get_strategy_manager),
) -> Any:
    return await _strategy_service_request(
        "PUT",
        f"/strategies/{name}/risk-settings",
        json=payload.model_dump(exclude_unset=True),
        fallback=lambda: _update_strategy_risk_settings_local(
            strategy_manager, name, payload
        ),
    )


@router.get("/api/strategies/{name}/status")
async def get_internal_strategy_status_api(
    name: str,
    user: Optional[str] = Depends(get_current_user),
    strategy_manager: StrategyManager = Depends(get_strategy_manager),
) -> Any:
    return await _strategy_service_request(
        "GET",
        f"/strategies/{name}/status",
        fallback=lambda: _get_strategy_status_local(strategy_manager, name),
    )


@router.post("/api/strategies/{name}/data-push")
async def request_strategy_data_push(
    name: str,
    payload: StrategyDataPushRequest,
    user: Optional[str] = Depends(get_current_user),
    strategy_manager: StrategyManager = Depends(get_strategy_manager),
) -> Any:
    return await _strategy_service_request(
        "POST",
        f"/strategies/{name}/data-push",
        json=payload.model_dump(exclude_none=True),
        fallback=lambda: _set_strategy_data_push_local(strategy_manager, name, payload),
    )


@router.put("/api/strategies/{name}")
async def update_strategy_api(
    name: str,
    updates: Dict[str, Any],
    user: Optional[str] = Depends(get_current_user),
    strategy_manager: StrategyManager = Depends(get_strategy_manager),
) -> Any:
    return await _strategy_service_request(
        "PUT",
        f"/strategies/{name}",
        json=dict(updates),
        fallback=lambda: _update_strategy_local(strategy_manager, name, dict(updates)),
    )


@router.post("/api/strategies/{name}/rename")
async def rename_strategy_api(
    name: str,
    payload: StrategyRenameRequest,
    user: Optional[str] = Depends(get_current_user),
    strategy_manager: StrategyManager = Depends(get_strategy_manager),
) -> Any:
    return await _strategy_service_request(
        "POST",
        f"/strategies/{name}/rename",
        json=payload.model_dump(exclude_none=True),
        fallback=lambda: _rename_strategy_local(strategy_manager, name, payload),
    )


@router.delete("/api/strategies/{name}")
async def delete_strategy_api(
    name: str,
    user: Optional[str] = Depends(get_current_user),
    strategy_manager: StrategyManager = Depends(get_strategy_manager),
) -> Any:
    return await _strategy_service_request(
        "DELETE",
        f"/strategies/{name}",
        fallback=lambda: _delete_strategy_local(strategy_manager, name),
    )


@router.post("/api/strategies/{name}/toggle")
async def toggle_strategy_api(
    name: str,
    enabled: bool,
    user: Optional[str] = Depends(get_current_user),
    strategy_manager: StrategyManager = Depends(get_strategy_manager),
) -> Any:
    return await _strategy_service_request(
        "POST",
        f"/strategies/{name}/toggle",
        json={"enabled": bool(enabled)},
        fallback=lambda: _toggle_strategy_local(strategy_manager, name, bool(enabled)),
    )


@router.post("/api/strategies/{name}/reset")
async def reset_strategy_data_api(
    name: str,
    user: Optional[str] = Depends(get_current_user),
    strategy_manager: StrategyManager = Depends(get_strategy_manager),
) -> Any:
    return await _strategy_service_request(
        "POST",
        f"/strategies/{name}/reset",
        fallback=lambda: _reset_strategy_local(strategy_manager, name),
    )


@router.get("/api/strategies/{name}/performance")
async def get_strategy_performance_api(
    name: str,
    range: str = "1d",
    start: Optional[str] = None,
    end: Optional[str] = None,
    user: Optional[str] = Depends(get_current_user),
    strategy_manager: StrategyManager = Depends(get_strategy_manager),
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
        fallback=lambda: _get_strategy_performance_local(
            strategy_manager, name, range, start, end
        ),
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
    strategy_manager: StrategyManager = Depends(get_strategy_manager),
) -> Any:
    params = {
        "limit": max(1, min(int(limit or 1), 1000)),
        "force_refresh": str(force_refresh).lower(),
    }
    return await _strategy_service_request(
        "GET",
        f"/strategies/{name}/executions",
        params=params,
        fallback=lambda: _get_strategy_executions_local(
            strategy_manager, name, params["limit"]
        ),
    )


@router.delete("/api/strategies/{name}/executions")
async def clear_strategy_executions(
    name: str,
    user: Optional[str] = Depends(get_current_user),
    strategy_manager: StrategyManager = Depends(get_strategy_manager),
) -> Any:
    return await _strategy_service_request(
        "DELETE",
        f"/strategies/{name}/executions",
        fallback=lambda: _clear_strategy_executions_local(strategy_manager, name),
    )
