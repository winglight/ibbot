"""Redis utilities for ibbot.

This module provides a thin async wrapper around ``redis.asyncio`` that exposes
application-specific helpers for queues, streams, and pub/sub handling.  The
APIs are designed so that callers can inject a pre-configured Redis client
(ideal for testing with ``fakeredis``) while production code lazily initialises
and reuses a shared connection pool.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import os
import uuid
import sys
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Iterable, Mapping, MutableMapping, Optional

try:  # redis is optional for a subset of test environments
    from redis.asyncio import Redis
    from redis.asyncio.client import PubSub
    from redis.asyncio.connection import ConnectionPool
except Exception:  # pragma: no cover - redis dependency is optional
    Redis = None  # type: ignore[assignment]
    PubSub = None  # type: ignore[assignment]
    ConnectionPool = None  # type: ignore[assignment]

try:  # import errors depend on redis availability
    from redis.exceptions import ConnectionError as RedisConnectionError
    from redis.exceptions import RedisError, ResponseError
except Exception:  # pragma: no cover - redis might be missing entirely
    class RedisError(Exception):
        """Fallback Redis error used when redis-py is unavailable."""

    class RedisConnectionError(RedisError):
        """Fallback connection error."""

    class ResponseError(RedisError):
        """Fallback response error."""


logger = logging.getLogger(__name__)

DEFAULT_REDIS_URL_ENV = "IBBOT_REDIS_URL"
DEFAULT_REDIS_URL = "redis://localhost:6379/0"
DEFAULT_COMMAND_QUEUE = "queue:ib:commands"
DEFAULT_MARKET_STREAM = "stream:ib:market"
DEFAULT_STREAM_GROUP = "group:ib:market"
DEFAULT_MARKET_CHANNEL = "pubsub:ib:market"


class IBCommandError(Exception):
    """Base exception for IB command request errors."""

    def __init__(self, message: str, *, command_id: str, payload: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.command_id = command_id
        self.payload = dict(payload or {})


class IBCommandTimeout(IBCommandError):
    """Raised when waiting for an IB command result times out."""


class IBCommandExecutionError(IBCommandError):
    """Raised when the IB command reports an execution failure."""

_pool_lock = asyncio.Lock()
_pool: ConnectionPool | None = None
_client_lock = asyncio.Lock()
_client: Redis | None = None

Callback = Callable[[str, str], Optional[Awaitable[None]]]


if sys.version_info < (3, 10):
    def _compat_dataclass(*args, **kwargs):
        kwargs.pop("slots", None)
        return dataclass(*args, **kwargs)
else:
    _compat_dataclass = dataclass


@_compat_dataclass(slots=True)
class StreamMessage:
    """Structured representation of a Redis stream message."""

    stream: str
    message_id: str
    data: Mapping[str, Any]


def _require_redis() -> None:
    if Redis is None or ConnectionPool is None:
        raise RuntimeError("redis.asyncio is not available; install redis>=4.2")


def _resolve_url(redis_url: str | None = None) -> str:
    if redis_url:
        return redis_url
    return os.getenv(DEFAULT_REDIS_URL_ENV, DEFAULT_REDIS_URL)


async def get_redis_pool(redis_url: str | None = None, **kwargs: Any) -> ConnectionPool:
    """Return the shared connection pool, creating it on first access."""

    _require_redis()
    url = _resolve_url(redis_url)
    global _pool
    if _pool is None:
        async with _pool_lock:
            if _pool is None:
                _pool = ConnectionPool.from_url(
                    url,
                    encoding="utf-8",
                    decode_responses=True,
                    **kwargs,
                )
    return _pool


async def get_redis_client(redis_url: str | None = None) -> Redis:
    """Return a cached Redis client bound to the shared pool."""

    _require_redis()
    global _client
    if _client is None:
        async with _client_lock:
            if _client is None:
                pool = await get_redis_pool(redis_url)
                _client = Redis(connection_pool=pool)
    return _client


async def close_global_client() -> None:
    """Close the cached Redis client and underlying pool (mainly for tests)."""

    global _client, _pool
    client = _client
    _client = None
    pool = _pool
    _pool = None
    if client is not None:
        try:
            await client.close()
        except Exception:  # pragma: no cover - depends on redis implementation
            logger.debug("Failed to close Redis client", exc_info=True)
    if pool is not None:
        try:
            await pool.disconnect()
        except Exception:  # pragma: no cover - depends on redis implementation
            logger.debug("Failed to disconnect Redis pool", exc_info=True)


async def _ensure_client(redis: Redis | None = None) -> Redis:
    if redis is not None:
        return redis
    return await get_redis_client()


async def redis_set(
    key: str,
    value: Any,
    *,
    redis: Redis | None = None,
    expire: int | None = None,
) -> bool:
    client = await _ensure_client(redis)
    result = await client.set(key, value, ex=expire)
    return bool(result)


async def redis_get(key: str, *, redis: Redis | None = None) -> Any:
    client = await _ensure_client(redis)
    return await client.get(key)


async def enqueue_ib_command(
    command: Mapping[str, Any],
    *,
    redis: Redis | None = None,
    queue_key: str = DEFAULT_COMMAND_QUEUE,
) -> int:
    client = await _ensure_client(redis)
    payload = json.dumps(command)
    length = await client.rpush(queue_key, payload)
    return int(length or 0)


def _normalise_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(key, (bytes, bytearray)):
            key = key.decode("utf-8", errors="ignore")
        else:
            key = str(key)
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8", errors="ignore")
        result[key] = value
    return result


def _parse_result_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return ""
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return value
    return value


async def request_ib_command(
    method: str,
    *,
    args: Iterable[Any] | None = None,
    kwargs: Mapping[str, Any] | None = None,
    timeout: float = 15,
    redis: Redis | None = None,
    queue_key: str = DEFAULT_COMMAND_QUEUE,
    result_prefix: str = "hash:ib:result",
    stream_key: str = "stream:ib:results",
) -> dict[str, Any]:
    """Enqueue an IB command and await its result from Redis.

    The function waits for the corresponding result hash ``{result_prefix}:{command_id}``
    to be populated by the IB command service. On success a dictionary mirroring
    the stored hash (with the ``result`` value JSON-decoded when possible) is
    returned. If the command reports a failure an :class:`IBCommandExecutionError`
    is raised, while timeouts raise :class:`IBCommandTimeout`.
    """

    client = await _ensure_client(redis)
    command_id = uuid.uuid4().hex
    payload: dict[str, Any] = {
        "command_id": command_id,
        "method": method,
        "args": list(args or []),
        "kwargs": dict(kwargs or {}),
    }
    await enqueue_ib_command(payload, redis=client, queue_key=queue_key)

    loop = asyncio.get_running_loop()
    timeout = max(0.0, float(timeout))
    deadline = loop.time() + timeout
    result_key = f"{result_prefix.rstrip(':')}:{command_id}"
    last_stream_id = "$"

    async def _read_hash() -> dict[str, Any]:
        data = await client.hgetall(result_key)
        if not data:
            return {}
        return _normalise_mapping(data)

    while True:
        if timeout == 0:
            remaining = 0.0
        else:
            remaining = deadline - loop.time()
        if remaining < 0:
            remaining = 0.0

        record = await _read_hash()
        status = record.get("status")
        if status:
            normalised_status = str(status).lower()
            record.setdefault("command_id", command_id)
            if "result" in record:
                record["result"] = _parse_result_value(record["result"])
            if normalised_status == "success":
                return record
            error_message = str(record.get("error") or "IB command failed")
            raise IBCommandExecutionError(
                error_message,
                command_id=command_id,
                payload=record,
            )

        if remaining <= 0:
            break

        block_ms = max(1, int(min(remaining, 1.0) * 1000))
        try:
            entries = await client.xread({stream_key: last_stream_id}, block=block_ms, count=1)
        except (ResponseError, RedisError, AttributeError):
            await asyncio.sleep(min(remaining, 0.1))
            continue

        if entries:
            _, stream_entries = entries[-1]
            if stream_entries:
                last_stream_id = stream_entries[-1][0]
        else:
            await asyncio.sleep(min(remaining, 0.1))

    raise IBCommandTimeout(
        f"Timed out waiting for IB command {command_id}",
        command_id=command_id,
        payload={"method": method, "command_id": command_id},
    )


async def publish_market_update(
    update: Mapping[str, Any],
    *,
    redis: Redis | None = None,
    channel: str = DEFAULT_MARKET_CHANNEL,
) -> int:
    client = await _ensure_client(redis)
    payload = json.dumps(update)
    receivers = await client.publish(channel, payload)
    return int(receivers or 0)


async def read_hash(
    key: str,
    *,
    redis: Redis | None = None,
    encoding: str = "utf-8",
) -> dict[str, Any]:
    """Read a Redis hash and normalise byte payloads to ``str`` values."""

    client = await _ensure_client(redis)
    raw = await client.hgetall(key)
    if not raw:
        return {}

    result: dict[str, Any] = {}
    for field, value in raw.items():
        if isinstance(field, (bytes, bytearray)):
            field = field.decode(encoding, errors="ignore")
        else:
            field = str(field)
        if isinstance(value, (bytes, bytearray)):
            value = value.decode(encoding, errors="ignore")
        result[field] = value
    return result


async def read_stream(
    *,
    stream_key: str = DEFAULT_MARKET_STREAM,
    group: str = DEFAULT_STREAM_GROUP,
    consumer: str | None = None,
    count: int | None = None,
    block: int | None = None,
    redis: Redis | None = None,
) -> list[StreamMessage]:
    client = await _ensure_client(redis)
    consumer_name = consumer or f"{group}-{os.getpid()}"
    try:
        entries = await client.xreadgroup(
            groupname=group,
            consumername=consumer_name,
            streams={stream_key: ">"},
            count=count,
            block=block,
        )
    except ResponseError as exc:
        logger.warning("Failed to read stream %s: %s", stream_key, exc)
        return []
    messages: list[StreamMessage] = []
    for stream_name, stream_entries in entries or []:
        for message_id, data in stream_entries:
            messages.append(StreamMessage(stream=stream_name, message_id=message_id, data=dict(data)))
    return messages


async def ack_stream(
    message_ids: Iterable[str],
    *,
    stream_key: str = DEFAULT_MARKET_STREAM,
    group: str = DEFAULT_STREAM_GROUP,
    redis: Redis | None = None,
) -> int:
    message_id_list = list(message_ids)
    if not message_id_list:
        return 0
    client = await _ensure_client(redis)
    acknowledged = await client.xack(stream_key, group, *message_id_list)
    return int(acknowledged or 0)


async def get_queue_length(
    queue_key: str = DEFAULT_COMMAND_QUEUE,
    *,
    redis: Redis | None = None,
) -> int:
    client = await _ensure_client(redis)
    length = await client.llen(queue_key)
    return int(length or 0)


def _extract_idle(entry: Any) -> int:
    if entry is None:
        return 0
    for attr in ("idle", "time_since_delivered"):
        if hasattr(entry, attr):
            value = getattr(entry, attr)
            if value is not None:
                return int(value)
    if isinstance(entry, Mapping):
        for key in ("idle", "time_since_delivered"):
            value = entry.get(key)
            if value is not None:
                return int(value)
    if isinstance(entry, (list, tuple)) and len(entry) >= 4:
        value = entry[2]
        if isinstance(value, (int, float)):
            return int(value)
    return 0


async def get_stream_metrics(
    *,
    stream_key: str = DEFAULT_MARKET_STREAM,
    group: str = DEFAULT_STREAM_GROUP,
    redis: Redis | None = None,
) -> Dict[str, int]:
    client = await _ensure_client(redis)
    try:
        pending_info = await client.xpending(stream_key, group)
    except ResponseError:
        return {"pending": 0, "stale_ms": 0}
    pending = 0
    if isinstance(pending_info, Mapping):
        pending = int(pending_info.get("pending", 0) or 0)
    elif isinstance(pending_info, Iterable):
        iterator = iter(pending_info)
        try:
            pending = int(next(iterator))
        except StopIteration:
            pending = 0
        except Exception:  # pragma: no cover - defensive conversion
            pending = 0
    stale_ms = 0
    if pending:
        try:
            details = await client.xpending_range(
                stream_key,
                group,
                min="-",
                max="+",
                count=1,
            )
        except ResponseError:
            details = None
        if details:
            stale_ms = _extract_idle(details[0])
    return {"pending": pending, "stale_ms": stale_ms}


class PubSubListener:
    """Async pub/sub listener with automatic reconnection support."""

    def __init__(
        self,
        *,
        redis_factory: Callable[[], Awaitable[Redis]] | None = None,
        reconnect_delay: float = 1.0,
    ) -> None:
        self._redis_factory = redis_factory or get_redis_client
        self._reconnect_delay = reconnect_delay
        self._callbacks: MutableMapping[str, list[Callback]] = defaultdict(list)
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._channel_event = asyncio.Event()

    def register_callback(self, channel: str, callback: Callback) -> None:
        self._callbacks[channel].append(callback)
        self._channel_event.set()

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="redis-pubsub-listener")

    async def stop(self) -> None:
        self._running = False
        self._channel_event.set()
        task = self._task
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self._task = None

    async def _run(self) -> None:
        while self._running:
            channels = tuple(self._callbacks.keys())
            if not channels:
                await self._wait_for_channels()
                continue
            try:
                redis = await self._redis_factory()
            except Exception:  # pragma: no cover - factory failure
                logger.exception("Failed to acquire Redis client for pubsub")
                await asyncio.sleep(self._reconnect_delay)
                continue
            pubsub: PubSub = redis.pubsub()  # type: ignore[assignment]
            subscribed: set[str] = set()
            try:
                await pubsub.subscribe(*channels)
                subscribed.update(channels)
                while self._running:
                    if self._channel_event.is_set():
                        self._channel_event.clear()
                        new_channels = [ch for ch in self._callbacks.keys() if ch not in subscribed]
                        if new_channels:
                            await pubsub.subscribe(*new_channels)
                            subscribed.update(new_channels)
                    try:
                        message = await pubsub.get_message(
                            ignore_subscribe_messages=True,
                            timeout=1.0,
                        )
                    except asyncio.CancelledError:
                        raise
                    except (RedisConnectionError, ConnectionError):
                        raise
                    if message is None:
                        continue
                    await self._dispatch(message)
            except asyncio.CancelledError:
                break
            except (RedisConnectionError, ConnectionError):
                if not self._running:
                    break
                logger.warning("Redis pubsub disconnected; reconnecting")
                await asyncio.sleep(self._reconnect_delay)
            finally:
                with contextlib.suppress(Exception):
                    await pubsub.close()
        self._channel_event.clear()

    async def _wait_for_channels(self) -> None:
        self._channel_event.clear()
        try:
            await asyncio.wait_for(self._channel_event.wait(), timeout=self._reconnect_delay)
        except asyncio.TimeoutError:
            pass

    async def _dispatch(self, message: Mapping[str, Any]) -> None:
        if message.get("type") != "message":
            return
        channel_raw = message.get("channel")
        data_raw = message.get("data")
        channel = channel_raw.decode() if isinstance(channel_raw, bytes) else str(channel_raw)
        payload = data_raw.decode() if isinstance(data_raw, bytes) else str(data_raw)
        callbacks = list(self._callbacks.get(channel, ()))
        for callback in callbacks:
            try:
                result = callback(channel, payload)
                if inspect.isawaitable(result):
                    await result
            except Exception:  # pragma: no cover - callback failures logged
                logger.exception("Error while handling pubsub message on %s", channel)


__all__ = [
    "StreamMessage",
    "PubSubListener",
    "get_redis_pool",
    "get_redis_client",
    "close_global_client",
    "redis_set",
    "redis_get",
    "enqueue_ib_command",
    "publish_market_update",
    "read_hash",
    "read_stream",
    "ack_stream",
    "get_queue_length",
    "get_stream_metrics",
]
