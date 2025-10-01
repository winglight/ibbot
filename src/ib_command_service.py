"""Helpers for interacting with the shared IB command service infrastructure."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from loguru import logger

from ibbot.config import resolve_redis_url
from ibbot.redis import ensure_redis

from .utils import ensure_local_timezone, now_local, parse_local_datetime

try:  # pragma: no cover - redis is optional for some test suites
    from redis.asyncio import Redis
except Exception:  # pragma: no cover - when redis is not installed
    Redis = None  # type: ignore

__all__ = [
    "IBCommandServiceConfig",
    "CommandServiceStatus",
    "load_ib_command_config",
    "ensure_ib_command_redis",
    "read_ib_command_service_health",
    "evaluate_service_health",
    "get_ib_command_service_status",
    "command_wait_timeout",
]


@dataclass(slots=True)
class IBCommandServiceConfig:
    """Runtime configuration for the IB command service integration."""

    redis_url: str
    service_name: str
    command_queue_key: str
    command_result_prefix: str
    command_result_ttl_seconds: int
    command_timeout_seconds: float
    wait_timeout_buffer_seconds: float
    result_stream_key: str
    health_hash_key: str
    health_stale_seconds: float


@dataclass(slots=True)
class CommandServiceStatus:
    """Represents the current availability of the IB command service."""

    connected: bool
    health: Optional[Dict[str, Any]]
    error: Optional[str]
    checked_at: datetime


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):  # pragma: no cover - defensive guard
        return default


def _int_env(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.getenv(name)
    if raw is None:
        return max(minimum, default)
    try:
        value = int(raw.strip() or default)
    except Exception:  # pragma: no cover - defensive guard
        return max(minimum, default)
    return max(minimum, value)


def load_ib_command_config() -> IBCommandServiceConfig:
    """Load configuration from the environment with sensible defaults."""

    redis_url = resolve_redis_url("IB_COMMAND")
    service_name = os.getenv("IB_COMMAND_SOURCE", "ui-gateway")
    command_queue_key = os.getenv("IB_COMMAND_QUEUE_KEY", "queue:ib:commands")
    command_result_prefix = os.getenv(
        "IB_COMMAND_RESULT_PREFIX", "hash:ib:result:"
    )
    command_result_ttl_seconds = _int_env(
        "IB_COMMAND_RESULT_TTL_SECONDS", 300, minimum=10
    )
    command_timeout_seconds = _float_env(
        "IB_COMMAND_TIMEOUT_SECONDS", 15.0
    )
    wait_timeout_buffer_seconds = _float_env(
        "IB_COMMAND_WAIT_BUFFER_SECONDS", 5.0
    )
    result_stream_key = os.getenv(
        "IB_COMMAND_RESULT_STREAM_KEY", "stream:ib:results"
    )
    health_hash_key = os.getenv(
        "IB_COMMAND_HEALTH_KEY", "hash:service:health:ib-command"
    )
    health_stale_seconds = _float_env(
        "IB_COMMAND_HEALTH_STALE_SECONDS", 180.0
    )

    return IBCommandServiceConfig(
        redis_url=redis_url,
        service_name=service_name,
        command_queue_key=command_queue_key,
        command_result_prefix=command_result_prefix,
        command_result_ttl_seconds=command_result_ttl_seconds,
        command_timeout_seconds=command_timeout_seconds,
        wait_timeout_buffer_seconds=wait_timeout_buffer_seconds,
        result_stream_key=result_stream_key,
        health_hash_key=health_hash_key,
        health_stale_seconds=health_stale_seconds,
    )


async def ensure_ib_command_redis(
    config: Optional[IBCommandServiceConfig] = None,
) -> Optional[Redis]:
    """Return a Redis client connected to the IB command datastore."""

    cfg = config or load_ib_command_config()
    try:
        return await ensure_redis(url=cfg.redis_url)
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.warning("Failed to obtain Redis client for IB commands", error=str(exc))
        return None


def _convert_health_mapping(mapping: Dict[str, Any]) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for key, value in mapping.items():
        if key in {"queue_depth", "pending", "inflight"}:
            try:
                data[key] = int(value)
            except (TypeError, ValueError):
                data[key] = value
        elif key.endswith("_seconds"):
            try:
                data[key] = float(value)
            except (TypeError, ValueError):
                data[key] = value
        else:
            data[key] = value
    return data


async def read_ib_command_service_health(
    redis: Optional[Redis],
    *,
    config: Optional[IBCommandServiceConfig] = None,
) -> Dict[str, Any]:
    """Read the service health hash published by the command worker."""

    cfg = config or load_ib_command_config()
    if redis is None:
        return {}

    try:
        mapping = await redis.hgetall(cfg.health_hash_key)
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.debug(
            "Failed to fetch IB command service health", error=str(exc)
        )
        return {}

    if not mapping:
        return {}

    return _convert_health_mapping(mapping)


def _coerce_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        try:
            return ensure_local_timezone(value)
        except Exception:
            return value
    if isinstance(value, str):
        return parse_local_datetime(value)
    return None


def evaluate_service_health(
    health: Optional[Dict[str, Any]],
    *,
    stale_seconds: float,
) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
    """Determine whether the command service is healthy."""

    if not health:
        return False, None, None

    payload = dict(health)
    last_error_raw = payload.get("last_error")
    last_error = str(last_error_raw).strip() if last_error_raw else None
    if last_error == "":
        last_error = None

    timestamps = []
    for field in ("last_seen", "last_success", "updated_at"):
        candidate = _coerce_datetime(payload.get(field))
        if candidate is not None:
            try:
                timestamps.append(ensure_local_timezone(candidate))
            except Exception:
                timestamps.append(candidate)

    age_seconds: Optional[float] = None
    if timestamps:
        try:
            newest = max(timestamps)
            age_seconds = max(0.0, (now_local() - newest).total_seconds())
            payload["age_seconds"] = age_seconds
        except Exception:  # pragma: no cover - defensive guard
            age_seconds = None

    status = str(payload.get("status") or "").lower()
    connected = False

    if status in {"ok", "healthy", "ready", "connected", "online"} and not last_error:
        connected = True
    elif age_seconds is not None and age_seconds <= stale_seconds and not last_error:
        connected = True

    return connected, payload, last_error


_STATUS_CACHE: Optional[CommandServiceStatus] = None
_STATUS_LOCK = asyncio.Lock()


async def get_ib_command_service_status(
    *,
    force: bool = False,
    config: Optional[IBCommandServiceConfig] = None,
) -> CommandServiceStatus:
    """Fetch (and optionally cache) the IB command service status."""

    global _STATUS_CACHE
    cfg = config or load_ib_command_config()

    async with _STATUS_LOCK:
        if _STATUS_CACHE is not None and not force:
            # Consider cached values fresh for a short interval to avoid
            # hammering Redis for repeated checks during a single request.
            delta = (now_local() - _STATUS_CACHE.checked_at).total_seconds()
            if delta <= 2.0:
                return _STATUS_CACHE

        redis = await ensure_ib_command_redis(cfg)
        if redis is None:
            status = CommandServiceStatus(
                connected=False,
                health=None,
                error="Redis unavailable",
                checked_at=now_local(),
            )
        else:
            health = await read_ib_command_service_health(redis, config=cfg)
            connected, payload, error = evaluate_service_health(
                health, stale_seconds=cfg.health_stale_seconds
            )
            status = CommandServiceStatus(
                connected=bool(connected),
                health=payload,
                error=error,
                checked_at=now_local(),
            )

        _STATUS_CACHE = status
        return status


def command_wait_timeout(config: Optional[IBCommandServiceConfig] = None) -> float:
    """Compute the maximum wait time for command completion."""

    cfg = config or load_ib_command_config()
    return cfg.command_timeout_seconds + cfg.wait_timeout_buffer_seconds

