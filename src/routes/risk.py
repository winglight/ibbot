"""HTTP proxy routes for the risk microservice."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger

from core.dependencies import get_core_container
from .auth import (
    audit_log_failure,
    audit_log_success,
    get_current_user,
    require_roles,
)
from src.models import (
    PositionSizingRequest,
    RiskConfigUpdateRequest,
    RiskRuleRequest,
)

if TYPE_CHECKING:  # pragma: no cover - imported for type checking only
    from src.risk_manager import RiskManager

router = APIRouter()

_risk_service_url_env = os.getenv("RISK_SERVICE_URL", "").strip()
RISK_SERVICE_URL = _risk_service_url_env.rstrip("/") if _risk_service_url_env else None
DEFAULT_RISK_SERVICE_URL = "http://localhost:8030"
RISK_SERVICE_TIMEOUT = float(os.getenv("RISK_SERVICE_TIMEOUT", "10.0"))

FallbackHandler = Callable[[], Awaitable[Any]]


def _risk_service_base_url() -> str:
    return RISK_SERVICE_URL or DEFAULT_RISK_SERVICE_URL


def _api_path(path: str) -> str:
    return path[4:] if path.startswith("/api") else path


def _extract_error_detail(response: httpx.Response) -> Any:
    try:
        payload = response.json()
    except ValueError:
        text = response.text.strip()
        return text or "Risk service error"
    if isinstance(payload, dict):
        for key in ("detail", "message", "error", "reason"):
            value = payload.get(key)
            if value:
                return value
    return payload


async def _risk_service_request(
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    json: Optional[Dict[str, Any]] = None,
    fallback: Optional[FallbackHandler] = None,
) -> Any:
    try:
        async with httpx.AsyncClient(
            base_url=_risk_service_base_url(),
            timeout=RISK_SERVICE_TIMEOUT,
        ) as client:
            response = await client.request(method, _api_path(path), params=params, json=json)
    except httpx.HTTPError as exc:
        logger.error("Risk service request failed", method=method, path=path, error=str(exc))
        if fallback is not None:
            logger.warning(
                "Risk service call failed; using embedded fallback",
                path=path,
            )
            return await fallback()
        raise HTTPException(status_code=503, detail="Risk service unavailable") from exc

    if response.status_code >= 400:
        detail = _extract_error_detail(response)
        raise HTTPException(status_code=response.status_code, detail=detail)

    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError as exc:  # pragma: no cover - defensive guard
        logger.error(
            "Risk service returned non-JSON payload", status=response.status_code
        )
        raise HTTPException(status_code=502, detail="Invalid response from risk service") from exc


def _resolve_risk_manager(request: Request) -> "RiskManager":
    try:
        container = get_core_container(request)
    except RuntimeError as exc:  # pragma: no cover - defensive guard
        raise HTTPException(status_code=503, detail="Risk manager unavailable") from exc

    manager = getattr(container, "risk_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="Risk manager unavailable")
    return manager


def _embedded_risk_config(request: Request) -> Dict[str, Any]:
    manager = _resolve_risk_manager(request)
    return manager.get_config()


def _embedded_update_risk_config(
    request: Request, payload: RiskConfigUpdateRequest
) -> Dict[str, Any]:
    manager = _resolve_risk_manager(request)
    updates = payload.model_dump(exclude_unset=True)
    return manager.update_config(updates)


async def _embedded_position_sizing(
    request: Request, payload: PositionSizingRequest
) -> Dict[str, Any]:
    manager = _resolve_risk_manager(request)
    result = await manager.position_sizing(
        symbol=payload.symbol,
        stop_distance=payload.stop_distance,
        rv_current=payload.rv_current,
        signal_tier=(payload.signal_tier or "A"),
        execution_metrics=payload.execution_metrics or {},
        rv_ref=payload.rv_ref,
    )

    contracts = int(result.contracts)
    stop_distance = float(result.stop_distance)
    point_value = float(result.point_value)
    risk_per_contract = stop_distance * point_value
    max_risk = contracts * risk_per_contract
    estimated_investment = contracts * point_value

    return {
        "success": True,
        "N": contracts,
        "contracts": contracts,
        "scaleVol": float(result.scale_vol),
        "scaleDd": float(result.scale_dd),
        "scaleSig": float(result.scale_sig),
        "scaleExec": float(result.scale_exec),
        "scaleTotal": float(result.scale_total),
        "stopDistance": stop_distance,
        "pointValue": point_value,
        "riskPerContract": risk_per_contract,
        "estimatedInvestment": estimated_investment,
        "maxRisk": max_risk,
        "R": float(result.R),
        "R_star": float(result.R_star),
        "explain": result.explanation,
        "explanation": result.explanation,
    }


@router.post("/api/risk_rules")
async def add_risk_rule(
    rule: RiskRuleRequest,
    request: Request,
    user: Optional[str] = Depends(require_roles("admin", "operator", action="risk.add_rule")),
) -> Any:
    try:
        result = await _risk_service_request(
            "POST",
            "/risk_rules",
            json=rule.model_dump(exclude_none=True),
        )
    except HTTPException as exc:
        await audit_log_failure(
            request,
            detail=str(getattr(exc, "detail", "risk rule add failed")),
            extra={"symbol": rule.symbol},
        )
        raise
    await audit_log_success(
        request,
        detail="risk rule added",
        extra={"symbol": rule.symbol, "rule_type": rule.rule_type},
    )
    return result


@router.get("/api/risk_rules")
async def get_risk_rules(user: Optional[str] = Depends(get_current_user)) -> Any:
    return await _risk_service_request("GET", "/risk_rules")


@router.put("/api/risk_rules/{symbol}")
async def update_risk_rule(
    symbol: str,
    rule: RiskRuleRequest,
    request: Request,
    user: Optional[str] = Depends(require_roles("admin", "operator", action="risk.update_rule")),
) -> Any:
    try:
        result = await _risk_service_request(
            "PUT",
            f"/risk_rules/{symbol}",
            json=rule.model_dump(exclude_none=True),
        )
    except HTTPException as exc:
        await audit_log_failure(
            request,
            detail=str(getattr(exc, "detail", "risk rule update failed")),
            extra={"symbol": symbol},
        )
        raise
    await audit_log_success(
        request,
        detail="risk rule updated",
        extra={"symbol": symbol},
    )
    return result


@router.delete("/api/risk_rules/{symbol}")
async def delete_risk_rule(
    symbol: str,
    request: Request,
    user: Optional[str] = Depends(require_roles("admin", "operator", action="risk.remove_rule")),
) -> Any:
    try:
        result = await _risk_service_request("DELETE", f"/risk_rules/{symbol}")
    except HTTPException as exc:
        await audit_log_failure(
            request,
            detail=str(getattr(exc, "detail", "risk rule removal failed")),
            extra={"symbol": symbol},
        )
        raise
    await audit_log_success(
        request,
        detail="risk rule removed",
        extra={"symbol": symbol},
    )
    return result


@router.get("/api/order_queue_status")
async def get_order_queue_status(user: Optional[str] = Depends(get_current_user)) -> Any:
    return await _risk_service_request("GET", "/order_queue_status")


@router.get("/api/risk-config")
async def get_risk_config(
    request: Request, user: Optional[str] = Depends(get_current_user)
) -> Any:
    async def _fallback() -> Any:
        return _embedded_risk_config(request)

    return await _risk_service_request("GET", "/risk-config", fallback=_fallback)


@router.put("/api/risk-config")
async def update_risk_config(
    payload: RiskConfigUpdateRequest,
    request: Request,
    user: Optional[str] = Depends(require_roles("admin", "operator", action="risk.update_config")),
) -> Any:
    updates = payload.model_dump(exclude_unset=True)

    async def _fallback() -> Any:
        return _embedded_update_risk_config(request, payload)

    try:
        result = await _risk_service_request(
            "PUT",
            "/risk-config",
            json=updates,
            fallback=_fallback,
        )
    except HTTPException as exc:
        await audit_log_failure(
            request,
            detail=str(getattr(exc, "detail", "risk config update failed")),
        )
        raise
    await audit_log_success(request, detail="risk config updated", extra={})
    return result


@router.post("/api/position-sizing")
async def api_position_sizing(
    payload: PositionSizingRequest,
    request: Request,
    user: Optional[str] = Depends(get_current_user),
) -> Any:
    payload_data = payload.model_dump(exclude_none=True)

    async def _fallback() -> Any:
        return await _embedded_position_sizing(request, payload)

    return await _risk_service_request(
        "POST",
        "/position-sizing",
        json=payload_data,
        fallback=_fallback,
    )


@router.get("/api/sizing/history")
async def get_sizing_history(
    symbol: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 100,
    user: Optional[str] = Depends(get_current_user),
) -> Any:
    params: Dict[str, Any] = {"limit": max(1, min(int(limit or 1), 500))}
    if symbol:
        params["symbol"] = symbol
    if since:
        params["since"] = since
    return await _risk_service_request("GET", "/sizing/history", params=params)


@router.get("/api/config/rules")
async def get_config_rules(user: Optional[str] = Depends(get_current_user)) -> Any:
    return await _risk_service_request("GET", "/config/rules")


@router.put("/api/config/rules")
async def update_config_rules(
    payload: Dict[str, Any],
    request: Request,
    user: Optional[str] = Depends(require_roles("admin", "operator", action="risk.update_rules")),
) -> Any:
    try:
        result = await _risk_service_request("PUT", "/config/rules", json=dict(payload))
    except HTTPException as exc:
        await audit_log_failure(
            request,
            detail=str(getattr(exc, "detail", "risk rules config update failed")),
        )
        raise
    await audit_log_success(request, detail="risk rules config updated", extra={})
    return result
