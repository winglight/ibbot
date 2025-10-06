"""Redis client utilities used across the application."""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from loguru import logger

try:  # redis is optional during unit tests
    from redis.asyncio import Redis
    from redis.asyncio.client import Pipeline
    from redis.exceptions import ResponseError
except Exception:  # pragma: no cover - redis not installed during some tests
    Redis = None  # type: ignore
    Pipeline = Any  # type: ignore
    ResponseError = Exception  # type: ignore[misc,assignment]

__all__ = [
    "RedisConfig",
    "init_redis",
    "get_redis_client",
    "ensure_redis",
    "create_pipeline",
    "hgetall",
    "hset",
    "hincrby",
    "hincrbyfloat",
    "zadd",
    "lrange",
    "lpush",
    "ltrim",
    "delete",
]


@dataclass(frozen=True)
class RedisConfig:
    """Simple container for Redis configuration."""

    url: Optional[str] = None
    host: str = "localhost"
    port: int = 6379
    db: int = 0
    password: Optional[str] = None
    ssl: bool = False


_client: Optional[Redis] = None
_client_lock = asyncio.Lock()
_STREAM_GROUP_DEFAULTS: Dict[str, List[str]] = {
    "stream:market-service": ["group:risk:market"],
    "stream:ib:results": ["group:risk:results"],
}


def _load_config() -> RedisConfig:
    url = os.getenv("REDIS_URL")
    if url:
        return RedisConfig(url=url)

    host = os.getenv("REDIS_HOST", "localhost")
    port_raw = os.getenv("REDIS_PORT", "6379")
    db_raw = os.getenv("REDIS_DB", "0")
    password = os.getenv("REDIS_PASSWORD")
    ssl_enabled = os.getenv("REDIS_SSL", "false").lower() in {"1", "true", "yes"}

    try:
        port = int(port_raw)
    except ValueError:
        port = 6379
    try:
        db = int(db_raw)
    except ValueError:
        db = 0

    return RedisConfig(
        url=None,
        host=host,
        port=port,
        db=db,
        password=password,
        ssl=ssl_enabled,
    )


async def init_redis(force: bool = False) -> Optional[Redis]:
    """Initialise the global Redis client if required."""

    global _client
    if Redis is None:  # pragma: no cover - redis optional for some tests
        logger.warning("redis-py is not installed; Redis cache disabled")
        return None

    if _client is not None and not force:
        return _client

    async with _client_lock:
        if _client is not None and not force:
            return _client

        config = _load_config()
        try:
            if config.url:
                client = Redis.from_url(
                    config.url,
                    encoding="utf-8",
                    decode_responses=True,
                )
            else:
                client = Redis(
                    host=config.host,
                    port=config.port,
                    db=config.db,
                    password=config.password,
                    ssl=config.ssl,
                    encoding="utf-8",
                    decode_responses=True,
                )

            await client.ping()
        except Exception as exc:
            logger.warning(f"Redis initialisation failed: {exc}")
            return None

        await _initialise_stream_groups(client)
        _client = client
        logger.info(
            "Redis client initialised",
            host=config.host,
            port=config.port,
            db=config.db,
        )
        return _client


def get_redis_client() -> Optional[Redis]:
    """Return the cached Redis client if initialised."""

    return _client


async def ensure_redis() -> Optional[Redis]:
    """Ensure a Redis client exists, attempting to create it if missing."""

    client = get_redis_client()
    if client is not None:
        return client
    return await init_redis()


def _load_stream_group_config() -> Dict[str, List[str]]:
    """Return desired stream->groups mapping from defaults/env configuration."""

    raw = os.getenv("REDIS_STREAM_GROUPS")
    if not raw:
        return dict(_STREAM_GROUP_DEFAULTS)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning(f"Invalid REDIS_STREAM_GROUPS JSON: {exc}")
        return dict(_STREAM_GROUP_DEFAULTS)

    config: Dict[str, List[str]] = {}
    for stream, groups in data.items():
        if isinstance(groups, str):
            normalised = [groups]
        elif isinstance(groups, Iterable):
            normalised = [str(item) for item in groups if str(item).strip()]
        else:
            logger.warning("Ignoring invalid group configuration for stream {}", stream)
            continue

        if normalised:
            config[str(stream)] = normalised

    # Merge defaults with custom configuration (custom overrides duplicates)
    merged = dict(_STREAM_GROUP_DEFAULTS)
    merged.update(config)
    return merged


async def _initialise_stream_groups(client: Redis) -> None:
    """Ensure required Redis streams and consumer groups exist."""

    config = _load_stream_group_config()
    if not config:
        return

    for stream, groups in config.items():
        for group in groups:
            await _ensure_consumer_group(client, stream, group)


async def _ensure_consumer_group(client: Redis, stream: str, group: str) -> None:
    """Create *group* for *stream* if it is missing."""

    try:
        await client.xgroup_create(stream, group, id="$", mkstream=True)
        logger.info("Created Redis consumer group", stream=stream, group=group)
    except ResponseError as exc:
        message = str(exc)
        if "BUSYGROUP" in message:
            return
        logger.warning(
            "Failed to create Redis consumer group", stream=stream, group=group, error=message
        )
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning(
            "Unexpected error creating Redis consumer group", stream=stream, group=group, error=str(exc)
        )


async def create_pipeline(transaction: bool = True) -> Optional[Pipeline]:
    """Create a Redis pipeline from the global client."""

    client = await ensure_redis()
    if client is None:
        return None
    return client.pipeline(transaction=transaction)


async def hgetall(key: str) -> Dict[str, str]:
    client = await ensure_redis()
    if client is None:
        return {}
    return await client.hgetall(key)


async def hset(key: str, mapping: Dict[str, Any]) -> bool:
    client = await ensure_redis()
    if client is None:
        return False
    if not mapping:
        return True
    await client.hset(key, mapping=mapping)
    return True


async def hincrby(key: str, field: str, amount: int = 1) -> None:
    client = await ensure_redis()
    if client is None:
        return
    await client.hincrby(key, field, amount)


async def hincrbyfloat(key: str, field: str, amount: float = 1.0) -> None:
    client = await ensure_redis()
    if client is None:
        return
    await client.hincrbyfloat(key, field, amount)


async def zadd(key: str, mapping: Dict[str, float]) -> None:
    client = await ensure_redis()
    if client is None or not mapping:
        return
    await client.zadd(key, mapping)


async def lrange(key: str, start: int, stop: int) -> list[str]:
    client = await ensure_redis()
    if client is None:
        return []
    return await client.lrange(key, start, stop)


async def lpush(key: str, *values: Any) -> None:
    client = await ensure_redis()
    if client is None or not values:
        return
    await client.lpush(key, *values)


async def ltrim(key: str, start: int, stop: int) -> None:
    client = await ensure_redis()
    if client is None:
        return
    await client.ltrim(key, start, stop)


async def delete(*keys: str) -> None:
    client = await ensure_redis()
    if client is None or not keys:
        return
    await client.delete(*keys)
