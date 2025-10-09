import os
from typing import Any, Dict, Mapping, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger

import core
from src.connection_manager import ConnectionConfig
from src.ib_command_health import resolve_ib_account_type
from .service_clients import ConnectionControlClient, ServiceClientError
from .auth import audit_log_failure, audit_log_success, get_current_user, require_roles

router = APIRouter()


_CONNECTION_CONTROL_CLIENT = ConnectionControlClient()
_CONNECTION_CONFIG_MODULE = "ib-connection"


def _get_connection_control_client() -> ConnectionControlClient:
    return _CONNECTION_CONTROL_CLIENT


def _get_config_provider():
    container = core.get_container()
    return getattr(container, "config_provider", None) if container else None


def _coerce_int(value: Any, default: int) -> int:
    try:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            cleaned = value.strip()
            if not cleaned:
                return int(default)
            return int(float(cleaned))
    except (TypeError, ValueError):
        return int(default)
    return int(default)


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if not cleaned:
            return default
        if cleaned in {"1", "true", "yes", "y", "on", "paper"}:
            return True
        if cleaned in {"0", "false", "no", "n", "off", "live"}:
            return False
    return default


def _normalise_connection_config(values: Mapping[str, Any]) -> Dict[str, Any]:
    fallback = ConnectionConfig.from_env()
    host = (
        values.get("host")
        or values.get("IBKR_HOST")
        or values.get("ibkr_host")
        or fallback.host
    )
    port = _coerce_int(
        values.get("port")
        or values.get("IBKR_PORT")
        or values.get("ibkr_port"),
        fallback.port,
    )
    client_id = _coerce_int(
        values.get("client_id")
        or values.get("IBKR_CLIENT_ID")
        or values.get("clientId"),
        fallback.client_id,
    )
    paper_trading = _coerce_bool(
        values.get("paper_trading")
        or values.get("paperTrading")
        or values.get("IBKR_PAPER_TRADING"),
        fallback.paper_trading,
    )

    return {
        "host": str(host or fallback.host),
        "port": int(port),
        "client_id": int(client_id),
        "paper_trading": bool(paper_trading),
    }


async def _load_config_from_provider() -> Dict[str, Any] | None:
    provider = _get_config_provider()
    if provider is None:
        return None

    try:
        values = await provider.get_module(_CONNECTION_CONFIG_MODULE)
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.debug("Failed to load connection config from provider: {}", exc)
        return None

    if not values:
        return None

    try:
        return _normalise_connection_config(values)
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.debug("Invalid connection config payload from provider: {}", exc)
        return None


async def _load_connection_config() -> Dict[str, Any]:
    provider_config = await _load_config_from_provider()
    if provider_config:
        return provider_config

    client = _get_connection_control_client()
    try:
        remote = await client.fetch_config()
    except ServiceClientError as exc:  # pragma: no cover - service failures are logged
        logger.debug("Failed to load connection config via service: {}", exc)
    else:
        if isinstance(remote, Mapping):
            try:
                return _normalise_connection_config(remote)
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.debug("Invalid connection config payload from service: {}", exc)

    fallback = ConnectionConfig.from_env().to_dict()
    return _normalise_connection_config(fallback)


@router.get("/api/connection_config")
async def get_connection_config():
    try:
        return await _load_connection_config()
    except Exception as exc:
        logger.error("Failed to load connection config: {}", exc)
        raise HTTPException(status_code=500, detail="Failed to get connection config") from exc


@router.post("/api/connect")
async def connect_ibkr(
    config: ConnectionConfig,
    request: Request,
    user: Optional[str] = Depends(require_roles("admin", "operator", action="connection.connect")),
):
    payload = {
        "host": config.host,
        "port": config.port,
        "client_id": config.client_id,
        "paper_trading": config.paper_trading,
    }

    client = _get_connection_control_client()

    try:
        result = await client.connect(payload)
    except ServiceClientError as exc:
        logger.error("Failed to dispatch connect command via service: {}", exc)
        await audit_log_failure(
            request,
            detail="failed to dispatch connect command via service",
            extra={"service": "connection_control", "error": str(exc)},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Connection service unavailable",
        ) from exc

    await audit_log_success(
        request,
        detail="connect command dispatched via service",
        extra={
            "paper_trading": bool(config.paper_trading),
            "service": "connection_control",
        },
    )

    response: Dict[str, Any] = {
        "success": True,
        "status": "dispatched",
        "service": "connection_control",
    }
    if result is not None:
        response["result"] = result
    return response


@router.post("/api/disconnect")
async def disconnect_ibkr(
    request: Request,
    user: Optional[str] = Depends(require_roles("admin", "operator", action="connection.disconnect")),
):
    client = _get_connection_control_client()

    try:
        result = await client.disconnect()
    except ServiceClientError as exc:
        logger.error("Failed to dispatch disconnect command via service: {}", exc)
        await audit_log_failure(
            request,
            detail="failed to dispatch disconnect command via service",
            extra={"service": "connection_control", "error": str(exc)},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Connection service unavailable",
        ) from exc

    await audit_log_success(
        request,
        detail="disconnect command dispatched via service",
        extra={"service": "connection_control", "result": result},
    )

    response: Dict[str, Any] = {
        "success": True,
        "status": "dispatched",
        "service": "connection_control",
    }
    if result is not None:
        response["result"] = result
    return response


@router.get("/api/connection_status")
async def get_connection_status(user: Optional[str] = Depends(get_current_user)):
    """Return the current IBKR connection status via the connection control service."""

    client = _get_connection_control_client()
    try:
        payload = await client.fetch_status()
    except ServiceClientError as exc:
        logger.warning("Connection service unavailable for status: {}", exc)
        return {
            "service": "connection_control",
            "connected": False,
            "account_type": resolve_ib_account_type(),
            "last_error": "connection service unavailable",
            "queue": None,
            "config": None,
        }

    if not isinstance(payload, Mapping):
        return {
            "service": "connection_control",
            "connected": False,
            "account_type": resolve_ib_account_type(),
            "last_error": "invalid status payload",
            "queue": None,
            "config": None,
        }

    response: Dict[str, Any] = {
        "service": "connection_control",
        "account_type": resolve_ib_account_type(),
        "connected": bool(payload.get("connected", False)),
        "status": payload.get("status"),
        "last_error": payload.get("last_error"),
        "config": None,
        "queue": None,
    }

    config = payload.get("config")
    if isinstance(config, Mapping):
        response["config"] = dict(config)

    queue_key = payload.get("queue_key")
    queue_depth = payload.get("queue_depth")
    if queue_key is not None or queue_depth is not None:
        response["queue"] = {
            "key": queue_key,
            "depth": queue_depth,
        }

    return response
