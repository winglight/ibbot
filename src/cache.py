"""Redis client utilities used across the application."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

from loguru import logger

try:  # redis is optional during unit tests
    from redis.asyncio import Redis
    from redis.asyncio.client import Pipeline
except Exception:  # pragma: no cover - redis not installed during some tests
    Redis = None  # type: ignore
    Pipeline = Any  # type: ignore

__all__ = [
    "RedisConfig",
    "init_redis",
    "get_redis_client",
    "ensure_redis",
    "close_redis",
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
    connect_timeout: Optional[float] = None
    ping_timeout: Optional[float] = None
    socket_timeout: Optional[float] = None


_client: Optional[Redis] = None
_client_lock = asyncio.Lock()

_UNSET = object()


def _read_timeout_setting(env_name: str, *, multiplier: float = 1.0):
    raw = os.getenv(env_name, "").strip()
    if not raw:
        return _UNSET
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid {} value {!r}; ignoring", env_name, raw)
        return _UNSET
    value *= multiplier
    if value <= 0:
        logger.warning("{} must be positive; disabling timeout", env_name)
        return None
    return value


def _parse_timeout_setting(*entries: tuple[str, float], default: Any = _UNSET):
    for env_name, multiplier in entries:
        value = _read_timeout_setting(env_name, multiplier=multiplier)
        if value is _UNSET:
            continue
        return value
    return default


_URL_ENV_VARS = (
    "REDIS_URL",
    "IBBOT_REDIS_URL",
    "ACCOUNT_CACHE_REDIS_URL",
    "IB_COMMAND_REDIS_URL",
    "IB_FEED_REDIS_URL",
    "STRATEGY_REDIS_URL",
    "NOTIFICATION_REDIS_URL",
    "DATA_LAYER_REDIS_URL",
)


def _load_config() -> RedisConfig:
    connect_timeout = _parse_timeout_setting(
        ("REDIS_CONNECT_TIMEOUT", 1.0),
        ("REDIS_CONNECT_TIMEOUT_MS", 0.001),
        default=1.0,
    )
    socket_timeout = _parse_timeout_setting(
        ("REDIS_SOCKET_TIMEOUT", 1.0),
        ("REDIS_SOCKET_TIMEOUT_MS", 0.001),
    )
    if socket_timeout is _UNSET:
        socket_timeout = connect_timeout if isinstance(connect_timeout, (int, float)) else None
    ping_timeout = _parse_timeout_setting(
        ("REDIS_PING_TIMEOUT", 1.0),
        ("REDIS_PING_TIMEOUT_MS", 0.001),
    )
    if ping_timeout is _UNSET:
        base = connect_timeout if isinstance(connect_timeout, (int, float)) else None
        ping_timeout = max(base or 0, 3.0)

    url = ""
    for env_var in _URL_ENV_VARS:
        value = os.getenv(env_var, "").strip()
        if value:
            url = value
            break
    if url:
        return RedisConfig(
            url=url,
            connect_timeout=connect_timeout if isinstance(connect_timeout, (int, float)) else None,
            ping_timeout=ping_timeout if isinstance(ping_timeout, (int, float)) else None,
            socket_timeout=socket_timeout if isinstance(socket_timeout, (int, float)) else None,
        )

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
        connect_timeout=connect_timeout if isinstance(connect_timeout, (int, float)) else None,
        ping_timeout=ping_timeout if isinstance(ping_timeout, (int, float)) else None,
        socket_timeout=socket_timeout if isinstance(socket_timeout, (int, float)) else None,
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
        client = None
        try:
            common_kwargs: Dict[str, Any] = {
                "encoding": "utf-8",
                "decode_responses": True,
            }
            if isinstance(config.connect_timeout, (int, float)):
                common_kwargs["socket_connect_timeout"] = config.connect_timeout
            if isinstance(config.socket_timeout, (int, float)):
                common_kwargs["socket_timeout"] = config.socket_timeout

            if config.url:
                client = Redis.from_url(
                    config.url,
                    **common_kwargs,
                )
            else:
                client = Redis(
                    host=config.host,
                    port=config.port,
                    db=config.db,
                    password=config.password,
                    ssl=config.ssl,
                    **common_kwargs,
                )

            ping_timeout = config.ping_timeout
            if isinstance(ping_timeout, (int, float)):
                await asyncio.wait_for(client.ping(), timeout=ping_timeout)
            else:
                await client.ping()
        except asyncio.TimeoutError:
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    pass
            timeout = config.ping_timeout if isinstance(config.ping_timeout, (int, float)) else None
            if timeout is not None:
                logger.warning(
                    "Redis initialisation failed: ping timed out after {:.2f}s",
                    timeout,
                )
            else:
                logger.warning("Redis initialisation failed: ping timed out")
            return None
        except Exception as exc:
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    pass
            logger.warning(f"Redis initialisation failed: {exc}")
            return None

        _client = client
        log_context: Dict[str, Any] = {
            "connect_timeout": config.connect_timeout,
            "ping_timeout": config.ping_timeout,
            "socket_timeout": config.socket_timeout,
        }
        if config.url:
            log_context["url"] = config.url
        else:
            log_context.update({
                "host": config.host,
                "port": config.port,
                "db": config.db,
            })
        logger.info(
            "Redis client initialised",
            **{k: v for k, v in log_context.items() if v is not None},
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


async def close_redis() -> None:
    """Close and clear the cached Redis client."""

    global _client
    client = _client
    _client = None
    if client is None:
        return
    try:
        await client.close()
    except Exception:  # pragma: no cover - depends on redis implementation
        logger.debug("Failed to close Redis client", exc_info=True)


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
