"""Redis helpers shared across standalone ibbot services."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

from loguru import logger

try:  # pragma: no cover - redis may be optional in some environments
    from redis.asyncio import Redis
except Exception:  # pragma: no cover - fallback when redis is not installed
    Redis = None  # type: ignore

from .config import resolve_redis_url

__all__ = [
    "RedisSettings",
    "ensure_redis",
    "create_redis",
    "close_redis",
    "enqueue_ib_command",
    "wait_for_command_result",
    "request_ib_command",
]


@dataclass(slots=True)
class RedisSettings:
    """Simple container describing how to connect to Redis."""

    url: str
    encoding: str = "utf-8"
    decode_responses: bool = True
    health_check_interval: int = 30


_redis_lock = asyncio.Lock()
_cached_redis: Optional[Redis] = None


async def _build_redis(settings: RedisSettings) -> Optional[Redis]:
    if Redis is None:  # pragma: no cover - defensive guard when redis is absent
        logger.debug("redis.asyncio is not available; skipping Redis initialisation")
        return None

    try:
        client = Redis.from_url(
            settings.url,
            encoding=settings.encoding,
            decode_responses=settings.decode_responses,
            health_check_interval=settings.health_check_interval,
        )
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Failed to create Redis client", error=str(exc))
        return None
    return client


async def ensure_redis(
    *,
    url: Optional[str] = None,
    settings: Optional[RedisSettings] = None,
    force: bool = False,
) -> Optional[Redis]:
    """Return a cached Redis client, creating one on-demand."""

    global _cached_redis

    async with _redis_lock:
        if _cached_redis is not None and not force:
            return _cached_redis

        resolved_url = url or (settings.url if settings else resolve_redis_url())
        settings = settings or RedisSettings(url=resolved_url)
        client = await _build_redis(settings)
        if client is not None:
            _cached_redis = client
        return client


async def create_redis(
    *, url: Optional[str] = None, settings: Optional[RedisSettings] = None
) -> Optional[Redis]:
    """Create a new Redis client without affecting the cached singleton."""

    resolved_url = url or (settings.url if settings else resolve_redis_url())
    settings = settings or RedisSettings(url=resolved_url)
    return await _build_redis(settings)


async def close_redis(client: Optional[Redis]) -> None:
    """Close and dispose of the provided Redis client."""

    if client is None:  # pragma: no cover - defensive guard
        return
    try:
        await client.close()
    except Exception:  # pragma: no cover - best effort cleanup
        pass


async def enqueue_ib_command(
    redis: Optional[Redis],
    command: str,
    payload: Optional[Dict[str, Any]] = None,
    *,
    queue_key: str = "queue:ib:commands",
    result_prefix: str = "hash:ib:result:",
    result_ttl: Optional[int] = None,
    source: Optional[str] = None,
) -> Optional[str]:
    """Push an IB command request onto the shared Redis queue."""

    if redis is None:
        return None

    message = {
        "command_id": str(uuid.uuid4()),
        "command": command,
        "payload": payload or {},
        "source": source or "account-snapshot",
        "created_at": time.time(),
    }

    try:
        await redis.rpush(queue_key, json.dumps(message, ensure_ascii=False))
        if result_ttl is not None and result_ttl > 0:
            await redis.expire(f"{result_prefix}{message['command_id']}", int(result_ttl))
    except Exception as exc:
        logger.warning(
            "Failed to enqueue IB command",
            command=command,
            error=str(exc),
        )
        return None

    return message["command_id"]


async def _decode_result(raw: Any) -> Optional[Dict[str, Any]]:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="ignore")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
            return {"status": "ok", "data": parsed}
        except Exception:
            return {"status": "ok", "data": raw}
    return None


async def wait_for_command_result(
    redis: Optional[Redis],
    command_id: str,
    *,
    result_prefix: str = "hash:ib:result:",
    timeout: float = 15.0,
    poll_interval: float = 0.5,
) -> Optional[Dict[str, Any]]:
    """Poll Redis for the result of a previously queued command."""

    if redis is None:
        return None

    deadline = time.monotonic() + max(0.0, timeout)
    result_key = f"{result_prefix}{command_id}"

    while True:
        try:
            raw = await redis.get(result_key)
            if raw is None:
                raw_hash = await redis.hgetall(result_key)
                if raw_hash:
                    raw = raw_hash
        except Exception as exc:
            logger.warning(
                "Failed to read IB command result",
                command_id=command_id,
                error=str(exc),
            )
            raw = None

        decoded = await _decode_result(raw)
        if decoded is not None:
            return decoded

        if time.monotonic() >= deadline:
            return None

        await asyncio.sleep(max(0.05, poll_interval))


async def request_ib_command(
    redis: Optional[Redis],
    command: str,
    payload: Optional[Dict[str, Any]] = None,
    *,
    queue_key: str = "queue:ib:commands",
    result_prefix: str = "hash:ib:result:",
    timeout: float = 15.0,
    result_ttl: Optional[int] = None,
    source: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Enqueue an IB command and wait for its result."""

    command_id = await enqueue_ib_command(
        redis,
        command,
        payload,
        queue_key=queue_key,
        result_prefix=result_prefix,
        result_ttl=result_ttl,
        source=source,
    )
    if not command_id:
        return None

    return await wait_for_command_result(
        redis,
        command_id,
        result_prefix=result_prefix,
        timeout=timeout,
    )
