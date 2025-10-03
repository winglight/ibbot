"""HTTP proxy routes for the risk microservice.

All CRUD operations are delegated to :class:`RiskServiceClient` which persists
state within the risk service itself.  The API still consults Redis caches for
read-heavy endpoints to preserve existing latency characteristics.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger

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

from src.ibbot.redis import get_redis_client
from src.routes.service_clients import (
    RiskServiceClient,
    ServiceClientError,
    ServiceResponseError,
    ServiceUnavailableError,
)

router = APIRouter()

RISK_RULES_CACHE_KEY = "json:risk:rules"
RISK_CONFIG_CACHE_KEY = "json:risk:config"

_RISK_SERVICE_CLIENT = RiskServiceClient()


def _get_risk_service_client() -> RiskServiceClient:
    return _RISK_SERVICE_CLIENT


async def _call_risk_service(
    operation: str,
    call: Callable[[RiskServiceClient], Awaitable[Any]],
) -> Any:
    client = _get_risk_service_client()
    try:
        return await call(client)
    except ServiceUnavailableError as exc:
        logger.error(
            "Risk service unavailable",
            operation=operation,
            error=str(exc),
        )
        raise HTTPException(status_code=503, detail="Risk service unavailable") from exc
    except ServiceResponseError as exc:
        detail = exc.detail or "Risk service error"
        logger.error(
            "Risk service responded with error",
            operation=operation,
            status=getattr(exc, "status_code", None),
            detail=detail,
        )
        raise HTTPException(status_code=getattr(exc, "status_code", 502), detail=detail) from exc
    except ServiceClientError as exc:
        logger.error(
            "Risk service request failed",
            operation=operation,
            error=str(exc),
        )
        raise HTTPException(status_code=502, detail="Risk service request failed") from exc


async def _load_cached_payload(key: str) -> Dict[str, Any] | None:
    try:
        redis = await get_redis_client()
    except Exception as exc:
        logger.warning("Redis unavailable for risk cache", error=str(exc))
        return None

    try:
        raw = await redis.get(key)
    except Exception as exc:
        logger.warning("Failed to read risk cache", key=key, error=str(exc))
        return None

    if not raw:
        return None

    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        logger.warning("Invalid JSON in risk cache", key=key, error=str(exc))
        return None

    if not isinstance(payload, dict):
        logger.warning("Unexpected payload type in risk cache", key=key, type=type(payload))
        return None

    return dict(payload)


async def _cached_risk_rules() -> Dict[str, Any] | None:
    payload = await _load_cached_payload(RISK_RULES_CACHE_KEY)
    if payload is None:
        return None

    payload.setdefault("success", True)
    payload.setdefault("source", payload.get("source", "redis"))
    return payload


async def _cached_risk_config() -> Dict[str, Any] | None:
    payload = await _load_cached_payload(RISK_CONFIG_CACHE_KEY)
    if payload is None:
        return None

    payload.setdefault("source", payload.get("source", "redis"))
    return payload


def _merge_service_command(
    command_id: str,
    service_payload: Any,
    *,
    defaults: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    response: dict[str, Any] = {"success": True, "command_id": command_id}
    if defaults:
        response.update({str(k): v for k, v in defaults.items()})
    if isinstance(service_payload, Mapping):
        response.update({str(k): v for k, v in service_payload.items()})
    elif service_payload is not None:
        response.setdefault("result", service_payload)
    return response


@router.post("/api/risk_rules")
async def add_risk_rule(
    rule: RiskRuleRequest,
    request: Request,
    user: Optional[str] = Depends(require_roles("admin", "operator", action="risk.add_rule")),
) -> Any:
    symbol = str(rule.symbol or "").strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")

    command_id = uuid.uuid4().hex

    try:
        service_payload = await _call_risk_service(
            "create_rule", lambda client: client.create_rule(rule.model_dump(exclude_none=True))
        )
    except HTTPException as exc:
        await audit_log_failure(
            request,
            detail=str(getattr(exc, "detail", "risk rule add failed")),
            extra={"symbol": symbol},
        )
        raise

    await audit_log_success(
        request,
        detail="risk rule added",
        extra={"symbol": symbol, "rule_type": rule.rule_type, "command_id": command_id},
    )
    return _merge_service_command(command_id, service_payload)


@router.get("/api/risk_rules")
async def get_risk_rules(user: Optional[str] = Depends(get_current_user)) -> Any:
    """Return configured risk rules, preferring the Redis cache when available."""

    cached = await _cached_risk_rules()
    if cached is not None:
        return cached
    return await _call_risk_service("fetch_rules", lambda client: client.fetch_rules())


@router.put("/api/risk_rules/{symbol}")
async def update_risk_rule(
    symbol: str,
    rule: RiskRuleRequest,
    request: Request,
    user: Optional[str] = Depends(require_roles("admin", "operator", action="risk.update_rule")),
) -> Any:
    symbol_key = str(symbol or "").strip()
    if not symbol_key:
        raise HTTPException(status_code=400, detail="symbol is required")

    command_id = uuid.uuid4().hex

    try:
        service_payload = await _call_risk_service(
            "update_rule",
            lambda client: client.update_rule(
                symbol_key, rule.model_dump(exclude_none=True)
            ),
        )
    except HTTPException as exc:
        await audit_log_failure(
            request,
            detail=str(getattr(exc, "detail", "risk rule update failed")),
            extra={"symbol": symbol_key},
        )
        raise

    await audit_log_success(
        request,
        detail="risk rule updated",
        extra={"symbol": symbol_key, "command_id": command_id},
    )
    return _merge_service_command(command_id, service_payload)


@router.delete("/api/risk_rules/{symbol}")
async def delete_risk_rule(
    symbol: str,
    request: Request,
    user: Optional[str] = Depends(require_roles("admin", "operator", action="risk.remove_rule")),
) -> Any:
    symbol_key = str(symbol or "").strip()
    if not symbol_key:
        raise HTTPException(status_code=400, detail="symbol is required")

    command_id = uuid.uuid4().hex

    try:
        service_payload = await _call_risk_service(
            "delete_rule", lambda client: client.delete_rule(symbol_key)
        )
    except HTTPException as exc:
        await audit_log_failure(
            request,
            detail=str(getattr(exc, "detail", "risk rule removal failed")),
            extra={"symbol": symbol_key},
        )
        raise

    await audit_log_success(
        request,
        detail="risk rule removed",
        extra={"symbol": symbol_key, "command_id": command_id},
    )
    defaults = {"removed": symbol_key}
    if isinstance(service_payload, Mapping) and "removed" in service_payload:
        defaults["removed"] = service_payload["removed"]
    return _merge_service_command(command_id, service_payload, defaults=defaults)


@router.get("/api/order_queue_status")
async def get_order_queue_status(user: Optional[str] = Depends(get_current_user)) -> Any:
    """Fetch the live order queue status via the risk service HTTP API."""

    return await _call_risk_service(
        "fetch_order_queue_status", lambda client: client.fetch_order_queue_status()
    )


@router.get("/api/risk-config")
async def get_risk_config(user: Optional[str] = Depends(get_current_user)) -> Any:
    """Return the risk configuration snapshot with cache-aware semantics."""

    cached = await _cached_risk_config()
    if cached is not None:
        return cached

    return await _call_risk_service("fetch_config", lambda client: client.fetch_config())


@router.put("/api/risk-config")
async def update_risk_config(
    payload: RiskConfigUpdateRequest,
    request: Request,
    user: Optional[str] = Depends(require_roles("admin", "operator", action="risk.update_config")),
) -> Any:
    command_id = uuid.uuid4().hex

    updates = payload.model_dump(exclude_unset=True)

    try:
        service_payload = await _call_risk_service(
            "update_config",
            lambda client: client.update_config(updates),
        )
    except HTTPException as exc:
        await audit_log_failure(
            request,
            detail=str(getattr(exc, "detail", "risk config update failed")),
        )
        raise

    await audit_log_success(
        request,
        detail="risk config updated",
        extra={"fields": sorted(updates.keys()), "command_id": command_id},
    )
    defaults = {"updated": sorted(updates.keys())}
    return _merge_service_command(command_id, service_payload, defaults=defaults)


@router.post("/api/position-sizing")
async def api_position_sizing(
    payload: PositionSizingRequest,
    user: Optional[str] = Depends(get_current_user),
) -> Any:
    payload_data = payload.model_dump(exclude_none=True)

    return await _call_risk_service(
        "position_sizing", lambda client: client.position_sizing(payload_data)
    )


@router.get("/api/sizing/history")
async def get_sizing_history(
    symbol: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 100,
    user: Optional[str] = Depends(get_current_user),
) -> Any:
    resolved_limit = max(1, min(int(limit or 1), 500))
    return await _call_risk_service(
        "fetch_sizing_history",
        lambda client: client.fetch_sizing_history(
            symbol=symbol,
            since=since,
            limit=resolved_limit,
        ),
    )


@router.get("/api/config/rules")
async def get_config_rules(user: Optional[str] = Depends(get_current_user)) -> Any:
    """Retrieve rules metadata directly from the risk service."""

    return await _call_risk_service(
        "fetch_config_rules", lambda client: client.fetch_config_rules()
    )


@router.put("/api/config/rules")
async def update_config_rules(
    payload: Dict[str, Any],
    request: Request,
    user: Optional[str] = Depends(require_roles("admin", "operator", action="risk.update_rules")),
) -> Any:
    command_id = uuid.uuid4().hex

    filtered: dict[str, dict[str, Any]] = {}
    symbols: list[str] = []
    for symbol, value in (payload or {}).items():
        if not isinstance(value, Mapping):
            continue
        symbol_key = str(symbol or "").strip()
        if not symbol_key:
            continue
        filtered[symbol_key] = {**dict(value)}
        filtered[symbol_key].setdefault("symbol", symbol_key)
        symbols.append(symbol_key)

    try:
        service_payload = await _call_risk_service(
            "update_config_rules",
            lambda client: client.update_config_rules(filtered),
        )
    except HTTPException as exc:
        await audit_log_failure(
            request,
            detail=str(getattr(exc, "detail", "risk rules config update failed")),
        )
        raise

    await audit_log_success(
        request,
        detail="risk rules config updated",
        extra={"symbols": sorted(symbols), "command_id": command_id},
    )
    defaults = {"symbols": sorted(symbols)}
    return _merge_service_command(command_id, service_payload, defaults=defaults)
