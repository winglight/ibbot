import os
import uuid
from typing import Any, Dict, Mapping, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger

import core
from src.connection_manager import ConnectionConfig
from src.ibbot.redis import DEFAULT_COMMAND_QUEUE, enqueue_ib_command
from src.ib_command_health import (
    IB_COMMAND_HEALTH_KEY,
    IB_COMMAND_HEALTH_STALE_AFTER_SEC,
    build_ib_connection_health_payload,
    resolve_ib_account_type,
)
from .service_clients import ConnectionControlClient, ServiceClientError
from .auth import audit_log_failure, audit_log_success, get_current_user, require_roles

router = APIRouter()


_CONNECTION_CONTROL_CLIENT = ConnectionControlClient()
_COMMAND_QUEUE_OVERRIDE = os.getenv("IB_COMMAND_QUEUE_KEY", "").strip() or None
_CONNECTION_CONFIG_MODULE = "ib-connection"


def _decode_redis_mapping(raw: Dict[Any, Any]) -> Dict[str, str]:
    """Convert Redis hash payload into a plain string dictionary."""

    result: Dict[str, str] = {}
    for key, value in raw.items():
        if key is None:
            continue
        if isinstance(key, (bytes, bytearray)):
            key = key.decode("utf-8", errors="ignore")
        else:
            key = str(key)
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8", errors="ignore")
        elif value is None:
            value = ""
        else:
            value = str(value)
        result[key] = value
    return result


async def _load_ib_command_health() -> Dict[str, Any] | None:
    """Load ib-command service health details from Redis if available."""

    container = core.get_container()
    redis_client = getattr(container, "redis_client", None) if container else None
    if redis_client is None:
        return None

    try:
        raw = await redis_client.hgetall(IB_COMMAND_HEALTH_KEY)
    except Exception as exc:  # pragma: no cover - redis access errors
        logger.debug("Failed to load ib-command health from Redis: {}", exc)
        return None

    if not raw:
        return None

    stale_after = (
        IB_COMMAND_HEALTH_STALE_AFTER_SEC
        if IB_COMMAND_HEALTH_STALE_AFTER_SEC > 0
        else 30.0
    )
    return build_ib_connection_health_payload(
        _decode_redis_mapping(raw),
        stale_after=stale_after,
    )


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


async def _resolve_command_queue_key() -> str:
    if _COMMAND_QUEUE_OVERRIDE:
        return _COMMAND_QUEUE_OVERRIDE

    provider = _get_config_provider()
    if provider is None:
        return DEFAULT_COMMAND_QUEUE

    try:
        queue_key = await provider.get(
            "ib-command",
            "queue_key",
            default=DEFAULT_COMMAND_QUEUE,
        )
    except Exception:  # pragma: no cover - config lookup failure
        return DEFAULT_COMMAND_QUEUE

    queue_key_str = str(queue_key or "").strip()
    return queue_key_str or DEFAULT_COMMAND_QUEUE


async def _enqueue_command(payload: Mapping[str, Any]) -> None:
    queue_key = await _resolve_command_queue_key()
    await enqueue_ib_command(payload, queue_key=queue_key)


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
        await client.update_config(payload)
    except ServiceClientError as exc:
        logger.error("Failed to update connection config via service: {}", exc)
        await audit_log_failure(request, detail="failed to persist connection config")
        raise HTTPException(status_code=502, detail="Failed to persist connection config") from exc

    command_id = uuid.uuid4().hex
    command = {
        "command_id": command_id,
        "method": "connect",
        "args": [],
        "kwargs": payload,
        "metadata": {
            "source": "api.connect",
            "submitted_by": user or "anonymous",
        },
    }

    try:
        await _enqueue_command(command)
    except Exception as exc:
        logger.error("Failed to enqueue connect command: {}", exc)
        await audit_log_failure(request, detail="failed to enqueue connect command")
        raise HTTPException(status_code=503, detail="Failed to enqueue connect command") from exc

    await audit_log_success(
        request,
        detail="connect command enqueued",
        extra={
            "command_id": command_id,
            "paper_trading": bool(config.paper_trading),
        },
    )

    return {
        "success": True,
        "status": "queued",
        "command_id": command_id,
    }


@router.post("/api/disconnect")
async def disconnect_ibkr(
    request: Request,
    user: Optional[str] = Depends(require_roles("admin", "operator", action="connection.disconnect")),
):
    command_id = uuid.uuid4().hex
    command = {
        "command_id": command_id,
        "method": "disconnect",
        "args": [],
        "kwargs": {},
        "metadata": {
            "source": "api.disconnect",
            "submitted_by": user or "anonymous",
        },
    }

    try:
        await _enqueue_command(command)
    except Exception as exc:
        logger.error("Failed to enqueue disconnect command: {}", exc)
        await audit_log_failure(request, detail="failed to enqueue disconnect command")
        raise HTTPException(status_code=503, detail="Failed to enqueue disconnect command") from exc

    await audit_log_success(
        request,
        detail="disconnect command enqueued",
        extra={"command_id": command_id},
    )

    return {
        "success": True,
        "status": "queued",
        "command_id": command_id,
    }


@router.get("/api/connection_status")
async def get_connection_status(user: Optional[str] = Depends(get_current_user)):
    """Return the current IBKR connection status based on ib-command health."""

    try:
        redis_health = await _load_ib_command_health()
        if redis_health:
            return redis_health

        return {
            "connected": False,
            "account_type": resolve_ib_account_type(),
            "last_seen": None,
            "queue_depth": None,
            "last_error": "ib-command health unavailable",
            "stale": True,
            "source": "redis",
        }
    except Exception as exc:
        logger.error("Failed to get connection status: {}", exc)
        raise HTTPException(status_code=500, detail="Failed to get connection status") from exc
