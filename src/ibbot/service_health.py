"""Utilities for publishing service health information to Redis."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

try:  # Optional during tests where redis is unavailable.
    from redis.asyncio import Redis
except Exception:  # pragma: no cover - redis optional in CI environments
    Redis = Any  # type: ignore[assignment]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _serialise(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, bytes)):
        return value.decode() if isinstance(value, bytes) else value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return str(value)


async def update_service_health(
    redis: Redis,
    service_name: str,
    *,
    healthy: bool | None = None,
    status: str | None = None,
    last_error: str | None = None,
    metrics: Mapping[str, Any] | None = None,
    key: str | None = None,
    logger: Any | None = None,
) -> None:
    """Persist a heartbeat for *service_name* into Redis.

    Parameters
    ----------
    redis:
        Connected Redis client used for the update.
    service_name:
        Logical name of the service.  The default hash key becomes
        ``hash:service:health:<service_name>`` when *key* is not provided.
    healthy:
        Optional boolean flag describing the health state.
    status:
        Optional textual status.  When omitted a value is derived from
        ``healthy`` when available.
    last_error:
        Optional error string to persist alongside the heartbeat.
    metrics:
        Additional key/value metrics to include in the Redis hash.  Values are
        coerced to strings.
    key:
        Explicit Redis hash key override.
    logger:
        Optional logger used when persisting the data fails.
    """

    hash_key = key or f"hash:service:health:{service_name}"
    mapping: dict[str, str] = {"last_seen": _now().isoformat()}

    derived_status: str | None = status
    if healthy is not None:
        mapping["healthy"] = "true" if healthy else "false"
        if derived_status is None:
            derived_status = "healthy" if healthy else "unhealthy"
    if derived_status is not None:
        mapping["status"] = derived_status

    if last_error is not None:
        mapping["last_error"] = last_error

    if metrics:
        for field, value in metrics.items():
            mapping[str(field)] = _serialise(value)

    try:
        await redis.hset(hash_key, mapping=mapping)
    except Exception:  # pragma: no cover - defensive guard
        if logger is not None:
            try:
                logger.warning(
                    "Failed to update service health", extra={"service": service_name, "data": mapping}
                )
            except Exception:
                pass


__all__ = ["update_service_health"]
