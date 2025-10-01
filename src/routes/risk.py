"""HTTP proxy routes for the risk microservice."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Iterable, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger

from core.dependencies import get_core_container
from src.database import RiskRule as DatabaseRiskRule, get_db_manager
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

if TYPE_CHECKING:  # pragma: no cover - imported for type checking only
    from src.risk_manager import RiskManager

router = APIRouter()

_risk_service_url_env = os.getenv("RISK_SERVICE_URL", "").strip()
RISK_SERVICE_URL = _risk_service_url_env.rstrip("/") if _risk_service_url_env else None
DEFAULT_RISK_SERVICE_URL = "http://localhost:8030"
RISK_SERVICE_TIMEOUT = float(os.getenv("RISK_SERVICE_TIMEOUT", "10.0"))
RISK_RULES_CACHE_KEY = "json:risk:rules"
RISK_CONFIG_CACHE_KEY = "json:risk:config"


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}


ENABLE_EMBEDDED_RISK_MANAGER = _env_flag("ENABLE_EMBEDDED_RISK_MANAGER", False)

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


def _resolve_risk_manager(request: Request) -> "RiskManager":
    try:
        container = get_core_container(request)
    except RuntimeError as exc:  # pragma: no cover - defensive guard
        raise HTTPException(status_code=503, detail="Risk manager unavailable") from exc

    manager = getattr(container, "risk_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="Risk manager unavailable")
    return manager


def _maybe_risk_manager(request: Request | None) -> "RiskManager" | None:
    if request is None:
        return None
    try:
        container = get_core_container(request)
    except RuntimeError:
        return None
    return getattr(container, "risk_manager", None)


def _normalise_rule_type(rule_type: Optional[str], auto_trailing: bool) -> str:
    resolved = (rule_type or ("trailing" if auto_trailing else "fixed")).strip().lower()
    if resolved not in {"fixed", "trailing"}:
        return "fixed"
    return resolved


def _normalise_atr_params(params: Any) -> Dict[str, Any]:
    if params is None:
        return {}
    if isinstance(params, dict):
        return dict(params)
    try:
        return dict(params)
    except Exception:
        return {}


def _serialize_risk_rule(rule: Any) -> Dict[str, Any] | None:
    symbol = getattr(rule, "symbol", None)
    if not symbol:
        return None

    auto_trailing = bool(getattr(rule, "auto_trailing", False))
    rule_type = _normalise_rule_type(getattr(rule, "rule_type", None), auto_trailing)
    atr_params = _normalise_atr_params(getattr(rule, "atr_params", None))

    created = getattr(rule, "timestamp", None) or getattr(rule, "created_at", None)
    if isinstance(created, datetime):
        created_at = created.isoformat()
    else:
        created_at = str(created) if created is not None else None

    return {
        "symbol": symbol,
        "stop_loss_price": getattr(rule, "stop_loss_price", None),
        "take_profit_price": getattr(rule, "take_profit_price", None),
        "max_loss_percent": getattr(rule, "max_loss_percent", None),
        "enabled": bool(getattr(rule, "enabled", True)),
        "max_time_span": getattr(rule, "max_time_span", None),
        "created_at": created_at,
        "rule_type": rule_type,
        "auto_trailing": auto_trailing,
        "atr_params": atr_params,
    }


def _build_rules_payload(records: Iterable[Any]) -> Dict[str, Dict[str, Any]]:
    payload: Dict[str, Dict[str, Any]] = {}
    for record in records or []:
        serialized = _serialize_risk_rule(record)
        if not serialized:
            continue
        payload[serialized["symbol"]] = serialized
    return payload


def _build_operation_response(
    snapshot: Dict[str, Any], *, symbol: Optional[str] = None
) -> Dict[str, Any]:
    response: Dict[str, Any] = {
        "success": True,
        "rules": snapshot.get("rules", {}),
        "updated_at": snapshot.get("updated_at"),
        "source": snapshot.get("source"),
        "count": snapshot.get("count", len(snapshot.get("rules", {}))),
    }
    if symbol:
        response["symbol"] = symbol
        rule_payload = snapshot.get("rules", {}).get(symbol)
        if rule_payload is not None:
            response["rule"] = rule_payload
    return response


async def _persist_risk_rules_snapshot(
    request: Request | None,
    *,
    source: str = "embedded",
    records: Iterable[Any] | None = None,
) -> Dict[str, Any]:
    if records is None:
        manager = _maybe_risk_manager(request) if ENABLE_EMBEDDED_RISK_MANAGER else None
        if manager is not None:
            try:
                records = manager.get_risk_rules().values()
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.warning("Failed to read rules from embedded risk manager", error=str(exc))
                records = None
        if records is None:
            db = get_db_manager()
            try:
                records = db.load_risk_rules()
            except Exception as exc:
                logger.error("Failed to load risk rules from database", error=str(exc))
                raise HTTPException(status_code=500, detail="Unable to load risk rules") from exc

    rules_payload = _build_rules_payload(records)
    timestamp = datetime.now(timezone.utc).isoformat()
    snapshot: Dict[str, Any] = {
        "success": True,
        "rules": rules_payload,
        "updated_at": timestamp,
        "source": source,
        "count": len(rules_payload),
    }

    try:
        redis = await get_redis_client()
    except Exception as exc:
        logger.warning("Redis unavailable for risk rules snapshot", error=str(exc))
    else:
        try:
            await redis.set(RISK_RULES_CACHE_KEY, json.dumps(snapshot))
        except Exception as exc:
            logger.warning("Failed to persist risk rules snapshot", error=str(exc))

    return snapshot


def _build_db_rule(
    symbol: str,
    *,
    stop_loss_price: Optional[float],
    take_profit_price: Optional[float],
    max_loss_percent: Optional[float],
    max_time_span: Optional[int],
    auto_trailing: bool,
    atr_params: Optional[Dict[str, Any]],
    rule_type: Optional[str],
    timestamp: Optional[str] = None,
    enabled: Optional[bool] = None,
) -> DatabaseRiskRule:
    normalized_type = _normalise_rule_type(rule_type, auto_trailing)
    atr_payload = _normalise_atr_params(atr_params)
    return DatabaseRiskRule(
        symbol=symbol,
        stop_loss_price=stop_loss_price,
        take_profit_price=take_profit_price,
        max_loss_percent=max_loss_percent,
        enabled=True if enabled is None else bool(enabled),
        max_time_span=max_time_span,
        timestamp=timestamp,
        auto_trailing=bool(auto_trailing or normalized_type == "trailing"),
        atr_params=atr_payload,
        rule_type=normalized_type,
    )


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


async def _embedded_add_risk_rule(
    request: Request, rule: RiskRuleRequest
) -> Dict[str, Any]:
    payload = rule.model_dump(exclude_none=True)
    symbol = payload.get("symbol")
    if not symbol:
        raise HTTPException(status_code=400, detail="Symbol is required")

    atr_params = _normalise_atr_params(payload.get("atr_params"))
    requested_rule_type = payload.get("rule_type")
    auto_trailing = _normalise_rule_type(requested_rule_type, False) == "trailing"

    manager = _maybe_risk_manager(request) if ENABLE_EMBEDDED_RISK_MANAGER else None
    if manager is not None:
        loop = asyncio.get_running_loop()

        def _call() -> bool:
            return manager.add_risk_rule(
                symbol=symbol,
                stop_loss_price=payload.get("stop_loss_price"),
                take_profit_price=payload.get("take_profit_price"),
                max_loss_percent=payload.get("max_loss_percent"),
                max_time_span=payload.get("max_time_span"),
                auto_trailing=auto_trailing,
                atr_params=atr_params,
                rule_type=requested_rule_type,
            )

        success = await loop.run_in_executor(None, _call)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to add risk rule")
    else:
        db_rule = _build_db_rule(
            symbol,
            stop_loss_price=payload.get("stop_loss_price"),
            take_profit_price=payload.get("take_profit_price"),
            max_loss_percent=payload.get("max_loss_percent"),
            max_time_span=payload.get("max_time_span"),
            auto_trailing=auto_trailing,
            atr_params=atr_params,
            rule_type=requested_rule_type,
            timestamp=datetime.now(timezone.utc).isoformat(),
            enabled=True,
        )
        db = get_db_manager()
        try:
            db.save_risk_rule(db_rule)
        except Exception as exc:
            logger.error("Failed to save risk rule", error=str(exc))
            raise HTTPException(status_code=500, detail="Failed to add risk rule") from exc

    snapshot = await _persist_risk_rules_snapshot(request, source="embedded")
    return _build_operation_response(snapshot, symbol=symbol)


def _parse_enabled_flag(request: Request) -> Optional[bool]:
    raw = request.query_params.get("enabled")
    if raw is None:
        return None
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in {"1", "true", "yes", "on", "enabled"}:
            return True
        if lowered in {"0", "false", "no", "off", "disabled"}:
            return False
    return None


async def _embedded_update_risk_rule(
    request: Request, symbol: str, rule: RiskRuleRequest
) -> Dict[str, Any]:
    payload = rule.model_dump(exclude_none=True)
    if payload.get("symbol"):
        symbol = payload["symbol"]

    enabled_flag = _parse_enabled_flag(request)
    atr_params = _normalise_atr_params(payload.get("atr_params"))
    requested_rule_type = payload.get("rule_type")
    auto_trailing = (
        _normalise_rule_type(requested_rule_type, False) == "trailing"
        if requested_rule_type is not None
        else None
    )

    manager = _maybe_risk_manager(request) if ENABLE_EMBEDDED_RISK_MANAGER else None
    if manager is not None:
        loop = asyncio.get_running_loop()

        def _call() -> bool:
            return manager.update_risk_rule(
                symbol=symbol,
                stop_loss_price=payload.get("stop_loss_price"),
                take_profit_price=payload.get("take_profit_price"),
                enabled=enabled_flag,
                max_time_span=payload.get("max_time_span"),
                auto_trailing=auto_trailing,
                atr_params=atr_params if "atr_params" in payload else None,
                rule_type=requested_rule_type,
            )

        success = await loop.run_in_executor(None, _call)
        if not success:
            raise HTTPException(status_code=404, detail="Risk rule not found")
    else:
        db = get_db_manager()
        try:
            existing_rules = {r.symbol: r for r in db.load_risk_rules()}
        except Exception as exc:
            logger.error("Failed to load risk rules", error=str(exc))
            raise HTTPException(status_code=500, detail="Failed to update risk rule") from exc

        existing = existing_rules.get(symbol)
        if existing is None:
            raise HTTPException(status_code=404, detail="Risk rule not found")

        stop_loss = payload.get("stop_loss_price", existing.stop_loss_price)
        take_profit = payload.get("take_profit_price", existing.take_profit_price)
        max_loss = payload.get("max_loss_percent", existing.max_loss_percent)
        max_time_span = payload.get("max_time_span", existing.max_time_span)
        rule_type_final = requested_rule_type or getattr(existing, "rule_type", None)
        auto_trailing_final = (
            auto_trailing
            if auto_trailing is not None
            else bool(getattr(existing, "auto_trailing", False))
        )
        atr_final = (
            atr_params
            if "atr_params" in payload
            else _normalise_atr_params(getattr(existing, "atr_params", None))
        )
        enabled_final = (
            bool(getattr(existing, "enabled", True))
            if enabled_flag is None
            else enabled_flag
        )
        timestamp = getattr(existing, "timestamp", None) or datetime.now(timezone.utc).isoformat()

        db_rule = _build_db_rule(
            symbol,
            stop_loss_price=stop_loss,
            take_profit_price=take_profit,
            max_loss_percent=max_loss,
            max_time_span=max_time_span,
            auto_trailing=auto_trailing_final,
            atr_params=atr_final,
            rule_type=rule_type_final,
            timestamp=timestamp,
            enabled=enabled_final,
        )
        try:
            db.save_risk_rule(db_rule)
        except Exception as exc:
            logger.error("Failed to persist risk rule", error=str(exc))
            raise HTTPException(status_code=500, detail="Failed to update risk rule") from exc

    snapshot = await _persist_risk_rules_snapshot(request, source="embedded")
    return _build_operation_response(snapshot, symbol=symbol)


async def _embedded_delete_risk_rule(request: Request, symbol: str) -> Dict[str, Any]:
    manager = _maybe_risk_manager(request) if ENABLE_EMBEDDED_RISK_MANAGER else None
    if manager is not None:
        loop = asyncio.get_running_loop()

        def _call() -> bool:
            return manager.remove_risk_rule(symbol)

        success = await loop.run_in_executor(None, _call)
        if not success:
            raise HTTPException(status_code=404, detail="Risk rule not found")
    else:
        db = get_db_manager()
        try:
            existing_rules = {r.symbol for r in db.load_risk_rules()}
        except Exception as exc:
            logger.error("Failed to load risk rules", error=str(exc))
            raise HTTPException(status_code=500, detail="Failed to delete risk rule") from exc

        if symbol not in existing_rules:
            raise HTTPException(status_code=404, detail="Risk rule not found")

        try:
            db.delete_risk_rule(symbol)
        except Exception as exc:
            logger.error("Failed to delete risk rule", error=str(exc))
            raise HTTPException(status_code=500, detail="Failed to delete risk rule") from exc

    snapshot = await _persist_risk_rules_snapshot(request, source="embedded")
    return _build_operation_response(snapshot, symbol=symbol)


@router.post("/api/risk_rules")
async def add_risk_rule(
    rule: RiskRuleRequest,
    request: Request,
    user: Optional[str] = Depends(require_roles("admin", "operator", action="risk.add_rule")),
) -> Any:
    async def _fallback() -> Any:
        return await _embedded_add_risk_rule(request, rule)

    try:
        if RISK_SERVICE_URL:
            result = await _risk_service_request(
                "POST",
                "/risk_rules",
                json=rule.model_dump(exclude_none=True),
                fallback=_fallback,
            )
        else:
            result = await _fallback()
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
    cached = await _cached_risk_rules()
    if cached is not None:
        return cached
    return await _risk_service_request("GET", "/risk_rules")


@router.put("/api/risk_rules/{symbol}")
async def update_risk_rule(
    symbol: str,
    rule: RiskRuleRequest,
    request: Request,
    user: Optional[str] = Depends(require_roles("admin", "operator", action="risk.update_rule")),
) -> Any:
    async def _fallback() -> Any:
        return await _embedded_update_risk_rule(request, symbol, rule)

    enabled_param = request.query_params.get("enabled")
    params = {"enabled": enabled_param} if enabled_param is not None else None

    try:
        if RISK_SERVICE_URL:
            result = await _risk_service_request(
                "PUT",
                f"/risk_rules/{symbol}",
                params=params,
                json=rule.model_dump(exclude_none=True),
                fallback=_fallback,
            )
        else:
            result = await _fallback()
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
    async def _fallback() -> Any:
        return await _embedded_delete_risk_rule(request, symbol)

    try:
        if RISK_SERVICE_URL:
            result = await _risk_service_request(
                "DELETE",
                f"/risk_rules/{symbol}",
                fallback=_fallback,
            )
        else:
            result = await _fallback()
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
    cached = await _cached_risk_config()
    if cached is not None:
        return cached

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
