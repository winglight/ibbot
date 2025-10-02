"""HTTP proxy routes for the risk microservice.

Write operations avoid synchronous HTTP calls by updating the Redis-backed
configuration that the risk service watches.  Read endpoints continue to use
the HTTP client for lightweight queries and include docstrings describing the
cache semantics for clarity.
"""

from __future__ import annotations

import json
import os
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
RISK_CONFIG_MODULE = os.getenv("RISK_CONFIG_MODULE", "risk-service")
RISK_CONFIG_HASH_KEY = os.getenv(
    "RISK_CONFIG_HASH_KEY",
    f"hash:config:{RISK_CONFIG_MODULE}",
)
RISK_CONFIG_UPDATE_CHANNEL = os.getenv(
    "RISK_CONFIG_UPDATE_CHANNEL", "pubsub:config:update"
)
RISK_RULES_FIELD = "rules"

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


async def _require_config_redis() -> Any:
    try:
        return await get_redis_client()
    except Exception as exc:
        logger.error("Redis unavailable for risk configuration", error=str(exc))
        raise HTTPException(status_code=503, detail="Risk configuration store unavailable")


def _stringify_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _parse_rules_from_config(values: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    rules: dict[str, dict[str, Any]] = {}
    raw_rules = values.get(RISK_RULES_FIELD)
    if isinstance(raw_rules, str) and raw_rules.strip():
        try:
            parsed = json.loads(raw_rules)
        except (TypeError, ValueError):
            parsed = []
        if isinstance(parsed, list):
            for item in parsed:
                if not isinstance(item, Mapping):
                    continue
                symbol = str(item.get("symbol") or "").strip()
                if not symbol:
                    continue
                payload = dict(item)
                payload["symbol"] = symbol
                rules[symbol] = payload
        elif isinstance(parsed, Mapping):
            for symbol, payload in parsed.items():
                if not isinstance(payload, Mapping):
                    continue
                symbol_str = str(symbol or "").strip()
                if not symbol_str:
                    continue
                data = dict(payload)
                data.setdefault("symbol", symbol_str)
                rules[symbol_str] = data

    for key, value in values.items():
        if not isinstance(key, str) or not key.startswith("rules."):
            continue
        parts = key.split(".", 2)
        if len(parts) < 3:
            continue
        _, symbol, field = parts[0], parts[1], parts[2]
        symbol = symbol.strip()
        if not symbol:
            continue
        bucket = rules.setdefault(symbol, {"symbol": symbol})
        bucket[field] = value

    return rules


async def _load_config_snapshot(redis: Any) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    try:
        values = await redis.hgetall(RISK_CONFIG_HASH_KEY)
    except Exception as exc:
        logger.error("Failed to read risk configuration", error=str(exc))
        raise HTTPException(status_code=503, detail="Failed to read risk configuration") from exc

    mapping = {str(key): str(value) for key, value in values.items()} if values else {}
    rules = _parse_rules_from_config(mapping)
    return mapping, rules


async def _persist_rules(redis: Any, rules: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    payload = []
    for symbol, rule in rules.items():
        if not symbol:
            continue
        entry = dict(rule)
        entry["symbol"] = symbol
        payload.append(entry)

    try:
        existing = await redis.hgetall(RISK_CONFIG_HASH_KEY)
    except Exception as exc:
        logger.error("Failed to load risk configuration before update", error=str(exc))
        raise HTTPException(status_code=503, detail="Failed to update risk rules") from exc

    try:
        serialised = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        logger.error("Failed to serialise risk rules", error=str(exc))
        raise HTTPException(status_code=400, detail="Invalid risk rule payload") from exc

    try:
        if existing:
            delete_fields = [key for key in existing if str(key).startswith("rules.")]
            if delete_fields:
                await redis.hdel(RISK_CONFIG_HASH_KEY, *delete_fields)
        await redis.hset(RISK_CONFIG_HASH_KEY, mapping={RISK_RULES_FIELD: serialised})
        await redis.publish(RISK_CONFIG_UPDATE_CHANNEL, RISK_CONFIG_MODULE)
    except Exception as exc:
        logger.error("Failed to persist risk rules", error=str(exc))
        raise HTTPException(status_code=503, detail="Failed to update risk rules") from exc

    return {entry["symbol"]: entry for entry in payload}


async def _persist_config_updates(redis: Any, updates: Mapping[str, Any]) -> None:
    to_set: dict[str, str] = {}
    to_remove: list[str] = []
    for key, value in updates.items():
        key_str = str(key).strip()
        if not key_str:
            continue
        if value is None:
            to_remove.append(key_str)
        else:
            to_set[key_str] = _stringify_value(value)

    try:
        if to_set:
            await redis.hset(RISK_CONFIG_HASH_KEY, mapping=to_set)
        if to_remove:
            await redis.hdel(RISK_CONFIG_HASH_KEY, *to_remove)
        if to_set or to_remove:
            await redis.publish(RISK_CONFIG_UPDATE_CHANNEL, RISK_CONFIG_MODULE)
    except Exception as exc:
        logger.error("Failed to persist risk configuration", error=str(exc))
        raise HTTPException(status_code=503, detail="Failed to update risk configuration") from exc


async def _cached_risk_config() -> Dict[str, Any] | None:
    payload = await _load_cached_payload(RISK_CONFIG_CACHE_KEY)
    if payload is None:
        return None

    payload.setdefault("source", payload.get("source", "redis"))
    return payload


@router.post("/api/risk_rules")
async def add_risk_rule(
    rule: RiskRuleRequest,
    request: Request,
    user: Optional[str] = Depends(require_roles("admin", "operator", action="risk.add_rule")),
) -> Any:
    symbol = str(rule.symbol or "").strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")

    redis = await _require_config_redis()
    payload = rule.model_dump(exclude_none=True)
    command_id = uuid.uuid4().hex

    try:
        _, rules = await _load_config_snapshot(redis)
        rules[symbol] = {**payload, "symbol": symbol}
        persisted = await _persist_rules(redis, rules)
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
    return {"success": True, "command_id": command_id, "rule": persisted.get(symbol, {})}


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

    redis = await _require_config_redis()
    command_id = uuid.uuid4().hex

    try:
        _, rules = await _load_config_snapshot(redis)
        if symbol_key not in rules:
            rules[symbol_key] = {"symbol": symbol_key}
        rules[symbol_key].update(rule.model_dump(exclude_none=True))
        persisted = await _persist_rules(redis, rules)
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
    return {"success": True, "command_id": command_id, "rule": persisted.get(symbol_key, {})}


@router.delete("/api/risk_rules/{symbol}")
async def delete_risk_rule(
    symbol: str,
    request: Request,
    user: Optional[str] = Depends(require_roles("admin", "operator", action="risk.remove_rule")),
) -> Any:
    symbol_key = str(symbol or "").strip()
    if not symbol_key:
        raise HTTPException(status_code=400, detail="symbol is required")

    redis = await _require_config_redis()
    command_id = uuid.uuid4().hex

    try:
        _, rules = await _load_config_snapshot(redis)
        removed = rules.pop(symbol_key, None)
        persisted = await _persist_rules(redis, rules)
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
    return {"success": True, "command_id": command_id, "removed": removed or symbol_key, "rules": persisted}


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
    updates = payload.model_dump(exclude_unset=True)
    redis = await _require_config_redis()
    command_id = uuid.uuid4().hex

    try:
        await _persist_config_updates(redis, updates)
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
    return {"success": True, "command_id": command_id, "updated": sorted(updates.keys())}


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
    params: Dict[str, Any] = {"limit": max(1, min(int(limit or 1), 500))}
    if symbol:
        params["symbol"] = symbol
    if since:
        params["since"] = since
    return await _call_risk_service(
        "fetch_sizing_history", lambda client: client.fetch_sizing_history(params)
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
    redis = await _require_config_redis()
    command_id = uuid.uuid4().hex

    updates: dict[str, dict[str, Any]] = {}
    for symbol, value in (payload or {}).items():
        if not isinstance(value, Mapping):
            continue
        symbol_key = str(symbol or "").strip()
        if not symbol_key:
            continue
        updates[symbol_key] = {**dict(value), "symbol": symbol_key}

    try:
        _, rules = await _load_config_snapshot(redis)
        rules.update(updates)
        persisted = await _persist_rules(redis, rules)
    except HTTPException as exc:
        await audit_log_failure(
            request,
            detail=str(getattr(exc, "detail", "risk rules config update failed")),
        )
        raise

    await audit_log_success(
        request,
        detail="risk rules config updated",
        extra={"symbols": sorted(updates.keys()), "command_id": command_id},
    )
    return {"success": True, "command_id": command_id, "rules": persisted}
