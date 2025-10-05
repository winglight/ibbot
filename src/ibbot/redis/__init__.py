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
import sys
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Iterable, Mapping, MutableMapping, Optional

from src.cache import close_redis as cache_close_redis
from src.cache import ensure_redis as cache_ensure_redis

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

SERVICE_METRICS_ROOT = "zset:service_metrics"
SERVICE_METRICS_INDEX_KEY = "set:service_metrics:index"
SERVICE_METRICS_SERVICE_INDEX_PREFIX = "set:service_metrics:service"
SERVICE_METRICS_METRIC_INDEX_PREFIX = "set:service_metrics:metric"
DEFAULT_SERVICE_METRICS_TTL = 24 * 60 * 60


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
    if Redis is None:
        raise RuntimeError("redis.asyncio is not available; install redis>=4.2")


def _prime_shared_client(redis_url: str | None) -> None:
    if not redis_url:
        return
    for env_var in ("REDIS_URL", DEFAULT_REDIS_URL_ENV):
        if not os.getenv(env_var):
            os.environ[env_var] = redis_url


async def get_redis_pool(redis_url: str | None = None, **kwargs: Any) -> ConnectionPool:
    """Return the connection pool associated with the shared Redis client."""

    client = await get_redis_client(redis_url)
    pool = getattr(client, "connection_pool", None)
    if ConnectionPool is not None:
        if not isinstance(pool, ConnectionPool):  # pragma: no cover - depends on redis implementation
            raise RuntimeError("Shared Redis client does not expose a ConnectionPool")
    elif pool is None:  # pragma: no cover - defensive when redis import differs
        raise RuntimeError("Shared Redis client does not expose a connection pool")
    if kwargs:
        logger.debug("Ignoring custom connection kwargs %s for shared Redis pool", sorted(kwargs.keys()))
    return pool


async def get_redis_client(redis_url: str | None = None) -> Redis:
    """Return the shared Redis client initialised via :mod:`src.cache`."""

    _require_redis()
    _prime_shared_client(redis_url)
    client = await cache_ensure_redis()
    if client is None:
        raise RuntimeError("Redis client is not available; ensure redis>=4.2 is installed")
    return client


async def close_global_client() -> None:
    """Close the shared Redis client (primarily for service shutdown/tests)."""

    await cache_close_redis()


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


def _slug_value(value: Any) -> str:
    text = str(value or "")
    if not text:
        return "unknown"
    result = [
        ch.lower() if ch.isalnum() or ch in {"-", "_"} else "_"
        for ch in text
    ]
    slug = "".join(result).strip("_")
    return slug or "unknown"


def _metric_key(service: Any, metric: Any) -> str:
    return f"{SERVICE_METRICS_ROOT}:{_slug_value(service)}:{_slug_value(metric)}"


def _split_metric_key(key: str) -> tuple[str, str]:
    parts = key.split(":")
    if len(parts) >= 4:
        return parts[2], parts[3]
    return "unknown", "metric"


def _normalise_metric_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        if value > 10_000_000_000:  # assume milliseconds
            value /= 1000.0
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if text:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                parsed = datetime.fromisoformat(text)
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                pass
    return datetime.now(timezone.utc)


async def record_service_metrics(
    metrics: Mapping[str, Any] | Iterable[Mapping[str, Any]],
    *,
    redis: Redis | None = None,
    ttl_seconds: int = DEFAULT_SERVICE_METRICS_TTL,
) -> None:
    entries: list[Mapping[str, Any]]
    if isinstance(metrics, Mapping):
        entries = [metrics]
    else:
        entries = [entry for entry in metrics if isinstance(entry, Mapping)]

    if not entries:
        return

    client = await _ensure_client(redis)
    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - ttl_seconds
    pipe = client.pipeline()

    for entry in entries:
        service = entry.get("service") or "unknown"
        metric = entry.get("metric") or "metric"
        value_raw = entry.get("value")
        try:
            value = float(value_raw)
        except (TypeError, ValueError):
            continue

        timestamp = _normalise_metric_timestamp(
            entry.get("timestamp") or entry.get("recorded_at")
        )
        score = timestamp.timestamp()
        payload: dict[str, Any] = {
            "timestamp": timestamp.isoformat(),
            "value": value,
            "service": service,
            "metric": metric,
        }
        for key in ("queue_key", "stream_key", "group", "name"):
            if entry.get(key) is not None:
                payload[key] = entry.get(key)
        details = entry.get("details")
        if isinstance(details, Mapping):
            payload["details"] = dict(details)
            for key in ("queue_key", "stream_key", "group"):
                if key not in payload and details.get(key) is not None:
                    payload[key] = details.get(key)

        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        metric_key = _metric_key(service, metric)
        pipe.zadd(metric_key, {encoded: score})
        pipe.zremrangebyscore(metric_key, "-inf", cutoff)
        pipe.expire(metric_key, ttl_seconds)

        pipe.sadd(SERVICE_METRICS_INDEX_KEY, metric_key)
        pipe.expire(SERVICE_METRICS_INDEX_KEY, ttl_seconds)

        service_index = f"{SERVICE_METRICS_SERVICE_INDEX_PREFIX}:{_slug_value(service)}"
        pipe.sadd(service_index, metric_key)
        pipe.expire(service_index, ttl_seconds)

        metric_index = f"{SERVICE_METRICS_METRIC_INDEX_PREFIX}:{_slug_value(metric)}"
        pipe.sadd(metric_index, metric_key)
        pipe.expire(metric_index, ttl_seconds)

    await pipe.execute()


async def _resolve_metric_keys(
    client: Redis,
    *,
    service: str | None = None,
    metric: str | None = None,
) -> list[str]:
    if service and metric:
        return [_metric_key(service, metric)]

    keys: set[str] = set()
    if service:
        index = f"{SERVICE_METRICS_SERVICE_INDEX_PREFIX}:{_slug_value(service)}"
        members = await client.smembers(index)
        keys.update(
            member.decode("utf-8") if isinstance(member, bytes) else str(member)
            for member in members
        )
    if metric:
        index = f"{SERVICE_METRICS_METRIC_INDEX_PREFIX}:{_slug_value(metric)}"
        members = await client.smembers(index)
        keys.update(
            member.decode("utf-8") if isinstance(member, bytes) else str(member)
            for member in members
        )
    if not service and not metric:
        members = await client.smembers(SERVICE_METRICS_INDEX_KEY)
        keys.update(
            member.decode("utf-8") if isinstance(member, bytes) else str(member)
            for member in members
        )
    return sorted(keys)


async def _prune_metric_key(client: Redis, key: str) -> None:
    service_slug, metric_slug = _split_metric_key(key)
    await client.srem(SERVICE_METRICS_INDEX_KEY, key)
    await client.srem(f"{SERVICE_METRICS_SERVICE_INDEX_PREFIX}:{service_slug}", key)
    await client.srem(f"{SERVICE_METRICS_METRIC_INDEX_PREFIX}:{metric_slug}", key)


async def load_service_metrics(
    start: datetime,
    end: datetime | None = None,
    *,
    service: str | None = None,
    metric: str | None = None,
    redis: Redis | None = None,
) -> list[dict[str, Any]]:
    client = await _ensure_client(redis)
    end = end or datetime.now(timezone.utc)
    start_ts = start.timestamp()
    end_ts = end.timestamp()

    keys = await _resolve_metric_keys(client, service=service, metric=metric)
    if not keys:
        return []

    results: list[dict[str, Any]] = []
    for key in keys:
        if not key:
            continue
        if not await client.exists(key):
            await _prune_metric_key(client, key)
            continue
        raw_entries = await client.zrangebyscore(key, start_ts, end_ts)
        for raw in raw_entries:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            timestamp_text = payload.get("timestamp") or payload.get("time")
            timestamp = _normalise_metric_timestamp(timestamp_text)
            payload["time"] = timestamp.isoformat()
            payload.setdefault("service", payload.get("service"))
            payload.setdefault("metric", payload.get("metric"))
            if not payload.get("service") or not payload.get("metric"):
                service_slug, metric_slug = _split_metric_key(key)
                payload.setdefault("service", service_slug)
                payload.setdefault("metric", metric_slug)
            results.append(payload)

    results.sort(key=lambda item: item.get("time", ""))
    return results


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
    "record_service_metrics",
    "load_service_metrics",
]
