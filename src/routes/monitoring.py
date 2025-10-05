"""Monitoring-centric API endpoints for dashboard views."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

from fastapi import APIRouter, HTTPException, Query

from src.db import ConfigRepository, ConfigRepositoryError
from src.ibbot.redis import (
    DEFAULT_COMMAND_QUEUE,
    DEFAULT_MARKET_STREAM,
    DEFAULT_STREAM_GROUP,
    RedisError,
    get_queue_length,
    get_redis_client,
    get_stream_metrics,
    load_service_metrics,
)

router = APIRouter()
logger = logging.getLogger(__name__)


QUEUE_MONITOR_HASH_KEY = "hash:service:queue-monitor"
_REDIS_UNAVAILABLE_DETAIL = {"error": "redis_unavailable", "message": "Redis 服务不可用"}
DEFAULT_MONITORING_REDIS_COMMAND_TIMEOUT = 2.0


def _load_monitoring_command_timeout() -> float:
    raw = os.getenv("MONITORING_REDIS_COMMAND_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_MONITORING_REDIS_COMMAND_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "Invalid MONITORING_REDIS_COMMAND_TIMEOUT value", extra={"value": raw}
        )
        return DEFAULT_MONITORING_REDIS_COMMAND_TIMEOUT
    if value <= 0:
        logger.warning(
            "MONITORING_REDIS_COMMAND_TIMEOUT must be positive", extra={"value": value}
        )
        return DEFAULT_MONITORING_REDIS_COMMAND_TIMEOUT
    return value


MONITORING_REDIS_COMMAND_TIMEOUT = _load_monitoring_command_timeout()


async def _await_redis_command(awaitable: Awaitable[Any], *, action: str | None = None) -> Any:
    """Await a Redis command with a timeout and uniform error handling."""

    timeout = MONITORING_REDIS_COMMAND_TIMEOUT
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except asyncio.TimeoutError as exc:
        logger.warning("Redis command timed out", extra={"action": action, "timeout": timeout})
        raise HTTPException(status_code=503, detail=dict(_REDIS_UNAVAILABLE_DETAIL)) from exc
    except TimeoutError as exc:  # pragma: no cover - defensive for synchronous APIs
        logger.warning("Redis command timed out (native)", extra={"action": action, "timeout": timeout})
        raise HTTPException(status_code=503, detail=dict(_REDIS_UNAVAILABLE_DETAIL)) from exc
    except (RedisError, ConnectionError) as exc:
        logger.warning("Redis command failed", extra={"action": action, "error": str(exc)})
        raise HTTPException(status_code=503, detail=dict(_REDIS_UNAVAILABLE_DETAIL)) from exc


@dataclass(slots=True)
class QueueTarget:
    """Description of a Redis list queue to summarise."""

    queue_key: str
    name: str | None = None


@dataclass(slots=True)
class StreamTarget:
    """Description of a Redis stream consumer group to summarise."""

    stream_key: str
    group: str
    name: str | None = None


def _normalise_queue_targets(raw: Sequence[str] | None) -> tuple[QueueTarget, ...]:
    if not raw:
        return (QueueTarget(queue_key=DEFAULT_COMMAND_QUEUE, name="IB Commands"),)
    result: list[QueueTarget] = []
    for entry in raw:
        token = (entry or "").strip()
        if not token:
            continue
        if ":" in token:
            queue_key, label = token.split(":", 1)
            queue_key = queue_key.strip()
            label = label.strip()
            result.append(QueueTarget(queue_key=queue_key, name=label or None))
        else:
            result.append(QueueTarget(queue_key=token))
    return tuple(result) or (QueueTarget(queue_key=DEFAULT_COMMAND_QUEUE, name="IB Commands"),)


def _normalise_stream_targets(raw: Sequence[str] | None) -> tuple[StreamTarget, ...]:
    if not raw:
        return (
            StreamTarget(
                stream_key=DEFAULT_MARKET_STREAM,
                group=DEFAULT_STREAM_GROUP,
                name="Market Stream",
            ),
        )
    result: list[StreamTarget] = []
    for entry in raw:
        token = (entry or "").strip()
        if not token:
            continue
        pieces = [segment.strip() for segment in token.split(":")]
        if len(pieces) < 2:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Stream targets must be provided as 'stream_key:group' or "
                    "'stream_key:group:label'"
                ),
            )
        stream_key, group = pieces[0], pieces[1]
        label = pieces[2] if len(pieces) > 2 else None
        result.append(StreamTarget(stream_key=stream_key, group=group, name=label))
    return tuple(result)


def _to_str(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode("utf-8")
        except Exception:  # pragma: no cover - decoding errors fall back to repr
            return repr(value)
    return str(value)


def _truncate_value(value: str, max_length: int = 200) -> str:
    if len(value) <= max_length:
        return value
    return value[: max_length - 1] + "…"


async def _sample_hash_fields(redis, key: str, *, limit: int = 3) -> tuple[list[dict[str, str]], bool, str | None]:
    entries: list[dict[str, str]] = []
    has_more = False
    error: str | None = None
    hscan = getattr(redis, "hscan", None)
    try:
        if callable(hscan):
            cursor: int | str = 0
            while True:
                cursor, data = await _await_redis_command(
                    hscan(key, cursor=cursor, count=max(limit * 2, 10)), action="hscan"
                )
                mapping: Mapping[Any, Any]
                if isinstance(data, Mapping):
                    mapping = data
                else:
                    mapping = {}
                for field, value in mapping.items():
                    entries.append(
                        {
                            "field": _to_str(field),
                            "value": _truncate_value(_to_str(value)),
                        }
                    )
                    if len(entries) >= limit:
                        break
                if (cursor in (0, "0")) or len(entries) >= limit:
                    has_more = cursor not in (0, "0")
                    break
        else:
            hgetall = getattr(redis, "hgetall", None)
            if callable(hgetall):
                data = await _await_redis_command(hgetall(key), action="hgetall")
                if isinstance(data, Mapping):
                    items = list(data.items())
                    has_more = len(items) > limit
                    for field, value in items[:limit]:
                        entries.append(
                            {
                                "field": _to_str(field),
                                "value": _truncate_value(_to_str(value)),
                            }
                        )
            else:
                error = "Redis client does not support hash scanning"
    except HTTPException:
        entries.clear()
        has_more = False
        raise
    except Exception as exc:  # pragma: no cover - depends on redis client implementation
        entries.clear()
        has_more = False
        error = str(exc)
    return entries[:limit], has_more, error


async def _collect_scan_keys(redis, *, pattern: str, limit: int = 5) -> tuple[list[str], bool, str | None]:
    keys: list[str] = []
    has_more = False
    error: str | None = None
    scan_iter = getattr(redis, "scan_iter", None)
    try:
        if callable(scan_iter):
            iterator = scan_iter(match=pattern, count=max(limit * 5, 20))
            while True:
                try:
                    raw = await _await_redis_command(iterator.__anext__(), action="scan_iter")
                except StopAsyncIteration:
                    break
                key = _to_str(raw)
                if key:
                    keys.append(key)
                if len(keys) >= limit + 1:
                    has_more = True
                    break
        else:
            scan = getattr(redis, "scan", None)
            if callable(scan):
                cursor: int | str = 0
                while True:
                    cursor, batch = await _await_redis_command(
                        scan(cursor=cursor, match=pattern, count=max(limit * 5, 20)),
                        action="scan",
                    )
                    for raw in batch or []:
                        key = _to_str(raw)
                        if key:
                            keys.append(key)
                    if len(keys) >= limit + 1:
                        has_more = True
                        break
                    if cursor in (0, "0"):
                        break
            else:
                error = "Redis client does not support key scanning"
    except HTTPException:
        keys.clear()
        has_more = False
        raise
    except Exception as exc:  # pragma: no cover - depends on redis client implementation
        keys.clear()
        has_more = False
        error = str(exc)
    return keys[:limit], has_more, error


async def _summarise_hash_category(
    redis,
    *,
    pattern: str,
    label_fn: Callable[[str], str],
    key_limit: int = 5,
    field_limit: int = 3,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "pattern": pattern,
        "samples": [],
        "has_more": False,
        "count": 0,
        "error": None,
    }
    if redis is None:
        summary["error"] = "Redis unavailable"
        return summary

    keys, has_more, error = await _collect_scan_keys(redis, pattern=pattern, limit=key_limit)
    if error:
        summary["error"] = error
        return summary

    summary["has_more"] = has_more
    samples: list[dict[str, Any]] = []
    for key in keys:
        entry: dict[str, Any] = {
            "key": key,
            "label": label_fn(key),
            "fields": [],
            "has_more_fields": False,
            "error": None,
        }
        fields, field_more, field_error = await _sample_hash_fields(redis, key, limit=field_limit)
        entry["fields"] = fields
        entry["has_more_fields"] = field_more
        if field_error:
            entry["error"] = field_error
        samples.append(entry)
    summary["samples"] = samples
    summary["count"] = len(samples)
    return summary


async def _summarise_config_modules(field_limit: int = 10) -> dict[str, Any]:
    repository = ConfigRepository(logger_name="routes.config.monitor")
    try:
        versions = await repository.load_versions()
    except ConfigRepositoryError as exc:
        return {"samples": [], "has_more": False, "count": 0, "error": str(exc)}

    samples: list[dict[str, Any]] = []
    for module in sorted(versions.keys()):
        try:
            snapshot = await repository.load_module(module)
        except ConfigRepositoryError as exc:
            samples.append(
                {
                    "key": module,
                    "label": module,
                    "fields": [],
                    "has_more_fields": False,
                    "error": str(exc),
                }
            )
            continue

        fields = [
            {"field": key, "value": value}
            for key, value in sorted(snapshot.values.items())
        ]
        has_more = len(fields) > field_limit
        samples.append(
            {
                "key": module,
                "label": module,
                "fields": fields[:field_limit],
                "has_more_fields": has_more,
                "error": None,
            }
        )

    return {"samples": samples, "has_more": False, "count": len(samples), "error": None}


async def _collect_data_fields(redis) -> dict[str, Any]:
    if redis is None:
        configs = await _summarise_config_modules()
        return {
            "configs": configs,
            "orders": {"samples": [], "has_more": False, "count": 0, "error": "Redis unavailable"},
            "subscriptions": {"samples": [], "has_more": False, "count": 0, "error": "Redis unavailable"},
        }

    def _order_label(key: str) -> str:
        return key.split(":", 2)[-1] if ":" in key else key

    def _subscription_label(key: str) -> str:
        return key.split(":", 2)[-1] if ":" in key else key

    configs_task = _summarise_config_modules()
    orders_task = _summarise_hash_category(
        redis,
        pattern="hash:ib:result:*",
        label_fn=_order_label,
    )
    subscriptions_task = _summarise_hash_category(
        redis,
        pattern="hash:market:snapshot:*",
        label_fn=_subscription_label,
    )

    results = await asyncio.gather(configs_task, orders_task, subscriptions_task, return_exceptions=True)

    summary: dict[str, Any] = {
        "configs": {"samples": [], "has_more": False, "count": 0, "error": None},
        "orders": {"samples": [], "has_more": False, "count": 0, "error": None},
        "subscriptions": {"samples": [], "has_more": False, "count": 0, "error": None},
    }

    keys = ("configs", "orders", "subscriptions")
    for key, result in zip(keys, results):
        if isinstance(result, HTTPException):
            raise result
        if isinstance(result, Exception):  # pragma: no cover - defensive guard
            summary[key]["error"] = str(result)
            continue
        summary[key] = result
    return summary


async def _load_snapshot_from_redis(redis) -> dict[str, Any] | None:
    try:
        payload = await _await_redis_command(
            redis.hgetall(QUEUE_MONITOR_HASH_KEY), action="hgetall_queue_monitor"
        )
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - redis connection issues
        raise HTTPException(status_code=503, detail=dict(_REDIS_UNAVAILABLE_DETAIL)) from exc

    if not payload:
        return None

    timestamp = payload.get("timestamp") or ""
    queues: list[dict[str, Any]] = []
    streams: list[dict[str, Any]] = []
    for key, raw in payload.items():
        if key == "timestamp":
            continue
        target = queues if key.startswith("queue:") else streams if key.startswith("stream:") else None
        if target is None:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        if isinstance(data, Mapping):
            target.append(dict(data))
    queues.sort(key=lambda item: item.get("name") or item.get("queue_key") or "")
    streams.sort(key=lambda item: item.get("name") or item.get("stream_key") or "")
    return {"timestamp": timestamp, "queues": queues, "streams": streams, "source": "snapshot"}


async def _build_queue_metrics(
    redis,
    queues: Iterable[QueueTarget],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for target in queues:
        try:
            length = await _await_redis_command(
                get_queue_length(queue_key=target.queue_key, redis=redis),
                action="queue_length",
            )
        except HTTPException:
            results.clear()
            raise
        except Exception as exc:  # pragma: no cover - redis connection issues
            results.clear()
            raise HTTPException(status_code=503, detail=dict(_REDIS_UNAVAILABLE_DETAIL)) from exc
        results.append(
            {
                "name": target.name or target.queue_key,
                "queue_key": target.queue_key,
                "length": length,
                "consumption_rate": None,
                "stale_seconds": None,
            }
        )
    return results


async def _build_stream_metrics(
    redis,
    streams: Iterable[StreamTarget],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for target in streams:
        try:
            metrics = await _await_redis_command(
                get_stream_metrics(
                    stream_key=target.stream_key,
                    group=target.group,
                    redis=redis,
                ),
                action="stream_metrics",
            )
        except HTTPException:
            results.clear()
            raise
        except Exception as exc:  # pragma: no cover - redis connection issues
            results.clear()
            raise HTTPException(status_code=503, detail=dict(_REDIS_UNAVAILABLE_DETAIL)) from exc
        pending = int(metrics.get("pending", 0) or 0)
        stale_seconds = float(metrics.get("stale_ms", 0) or 0) / 1000.0
        results.append(
            {
                "name": target.name or target.stream_key,
                "stream_key": target.stream_key,
                "group": target.group,
                "pending": pending,
                "consumption_rate": None,
                "stale_seconds": stale_seconds,
            }
        )
    return results


def _build_monitoring_payload(
    *,
    queue_targets: Sequence[QueueTarget],
    stream_targets: Sequence[StreamTarget],
    queues: Sequence[Mapping[str, Any]],
    streams: Sequence[Mapping[str, Any]],
    data_fields: Mapping[str, Any],
    source: str,
    errors: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "timestamp": datetime.utcnow().isoformat(),
        "queues": list(queues),
        "streams": list(streams),
        "source": source,
        "data_fields": data_fields,
        "requested_queues": [target.queue_key for target in queue_targets],
        "requested_streams": [
            {"stream_key": target.stream_key, "group": target.group} for target in stream_targets
        ],
    }
    if errors:
        payload["errors"] = list(errors)
    return payload


@router.get("/api/monitoring/queues")
async def get_queue_metrics(
    queues: Sequence[str] | None = Query(default=None, alias="queue"),
    streams: Sequence[str] | None = Query(default=None, alias="stream"),
) -> Mapping[str, Any]:
    """Return queue and stream metrics for dashboard consumption."""

    queue_targets = _normalise_queue_targets(queues)
    stream_targets = _normalise_stream_targets(streams)
    errors: list[dict[str, Any]] = []

    try:
        redis = await get_redis_client()
    except Exception as exc:  # pragma: no cover - depends on runtime redis availability
        logger.warning("Redis client unavailable for monitoring queues", exc_info=True)
        data_fields = await _collect_data_fields(None)
        errors.append({"type": "redis_unavailable", "message": "Redis 服务不可用"})
        return _build_monitoring_payload(
            queue_targets=queue_targets,
            stream_targets=stream_targets,
            queues=[],
            streams=[],
            data_fields=data_fields,
            source="unavailable",
            errors=errors,
        )

    snapshot: dict[str, Any] | None = None
    try:
        snapshot = await _load_snapshot_from_redis(redis)
    except HTTPException as exc:
        if exc.status_code != 503:
            raise
        logger.warning("Monitoring snapshot unavailable due to Redis timeout")
        errors.append({"type": "redis_timeout", "context": "snapshot", "message": "Redis 请求超时"})
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Failed to load monitoring snapshot", exc_info=True)
        errors.append({"type": "redis_error", "context": "snapshot", "message": str(exc)})

    try:
        data_fields = await _collect_data_fields(redis)
    except HTTPException as exc:
        if exc.status_code != 503:
            raise
        logger.warning("Monitoring data field summary timed out")
        errors.append({"type": "redis_timeout", "context": "data_fields", "message": "Redis 请求超时"})
        data_fields = await _collect_data_fields(None)
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Failed to collect monitoring data fields", exc_info=True)
        errors.append({"type": "redis_error", "context": "data_fields", "message": str(exc)})
        data_fields = await _collect_data_fields(None)

    if snapshot:
        payload = dict(snapshot)
        payload["requested_queues"] = [target.queue_key for target in queue_targets]
        payload["requested_streams"] = [
            {"stream_key": target.stream_key, "group": target.group}
            for target in stream_targets
        ]
        payload["data_fields"] = data_fields
        if errors:
            payload["errors"] = errors
        return payload

    queues_payload: list[dict[str, Any]] = []
    streams_payload: list[dict[str, Any]] = []

    try:
        queues_payload = await _build_queue_metrics(redis, queue_targets)
    except HTTPException as exc:
        if exc.status_code != 503:
            raise
        logger.warning("Queue metric collection timed out")
        errors.append({"type": "redis_timeout", "context": "queues", "message": "Redis 请求超时"})
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Queue metric collection failed", exc_info=True)
        errors.append({"type": "redis_error", "context": "queues", "message": str(exc)})

    try:
        streams_payload = await _build_stream_metrics(redis, stream_targets)
    except HTTPException as exc:
        if exc.status_code != 503:
            raise
        logger.warning("Stream metric collection timed out")
        errors.append({"type": "redis_timeout", "context": "streams", "message": "Redis 请求超时"})
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Stream metric collection failed", exc_info=True)
        errors.append({"type": "redis_error", "context": "streams", "message": str(exc)})

    if queues_payload and streams_payload:
        source = "live"
    elif queues_payload or streams_payload:
        source = "partial"
    else:
        source = "degraded"

    return _build_monitoring_payload(
        queue_targets=queue_targets,
        stream_targets=stream_targets,
        queues=queues_payload,
        streams=streams_payload,
        data_fields=data_fields,
        source=source,
        errors=errors,
    )


@router.post("/api/monitoring/queues/clear-cache")
async def clear_monitoring_redis_cache() -> Mapping[str, Any]:
    """Flush all keys in the configured Redis database."""

    try:
        redis = await get_redis_client()
    except Exception as exc:  # pragma: no cover - depends on redis availability
        logger.exception("Redis client unavailable for cache clear")
        raise HTTPException(status_code=503, detail="Redis 客户端不可用") from exc

    try:
        await redis.flushdb()
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.exception("Failed to flush Redis cache")
        raise HTTPException(status_code=500, detail="清理 Redis 缓存失败") from exc

    return {"success": True, "message": "Redis 缓存已清理"}


_WINDOW_PATTERN = re.compile(r"^(?P<value>\d+)(?P<unit>[smhd])$", re.IGNORECASE)


def _parse_window(window: str) -> timedelta:
    token = window.strip()
    match = _WINDOW_PATTERN.match(token)
    if not match:
        raise HTTPException(status_code=400, detail="window must be a positive duration like 15m or 4h")
    value = int(match.group("value"))
    if value <= 0:
        raise HTTPException(status_code=400, detail="window must be greater than zero")
    unit = match.group("unit").lower()
    if unit == "s":
        return timedelta(seconds=value)
    if unit == "m":
        return timedelta(minutes=value)
    if unit == "h":
        return timedelta(hours=value)
    if unit == "d":
        return timedelta(days=value)
    raise HTTPException(status_code=400, detail=f"Unsupported window unit: {unit}")


def _normalise_aggregate(value: str) -> str:
    token = (value or "").strip().lower() or "sum"
    if token in {"sum", "total"}:
        return "sum"
    if token in {"avg", "mean", "average"}:
        return "avg"
    if token == "min":
        return "min"
    if token == "max":
        return "max"
    if token == "count":
        return "count"
    raise HTTPException(status_code=400, detail=f"Unsupported aggregate '{value}'")


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            normalised = raw.replace(" ", "T")
            try:
                return datetime.fromisoformat(normalised)
            except ValueError:
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
                    try:
                        return datetime.strptime(raw, fmt)
                    except ValueError:
                        continue
    return None


def _parse_metric_time(value: Any) -> datetime:
    timestamp = _coerce_datetime(value)
    if timestamp is not None:
        if timestamp.tzinfo is None:
            return timestamp.replace(tzinfo=timezone.utc)
        return timestamp.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric > 10_000_000_000:
            numeric /= 1000.0
        return datetime.fromtimestamp(numeric, tz=timezone.utc)
    return datetime.now(timezone.utc)


def _aggregate_metric_entries(
    entries: Iterable[Mapping[str, Any]], *, aggregate: str
) -> list[dict[str, Any]]:
    buckets: dict[tuple[Any, ...], list[float]] = defaultdict(list)
    has_service = False
    has_metric = False
    has_queue = False
    has_stream = False
    has_group = False

    for entry in entries:
        try:
            numeric = float(entry.get("value"))
        except (TypeError, ValueError):
            continue

        bucket_time = _parse_metric_time(entry.get("time") or entry.get("timestamp"))
        service = entry.get("service") or None
        metric = entry.get("metric") or None
        queue_key = entry.get("queue_key") or None
        stream_key = entry.get("stream_key") or None
        group = entry.get("group") or entry.get("group_name") or None

        details = entry.get("details") if isinstance(entry.get("details"), Mapping) else {}
        if queue_key is None:
            queue_key = details.get("queue_key")
        if stream_key is None:
            stream_key = details.get("stream_key")
        if group is None:
            group = details.get("group")

        has_service = has_service or service is not None
        has_metric = has_metric or metric is not None
        has_queue = has_queue or queue_key is not None
        has_stream = has_stream or stream_key is not None
        has_group = has_group or group is not None

        key = (bucket_time, service, metric, queue_key, stream_key, group)
        buckets[key].append(numeric)

    def _apply(values: list[float]) -> float | None:
        if not values:
            return None
        if aggregate == "sum":
            return float(sum(values))
        if aggregate == "avg":
            return float(sum(values) / len(values))
        if aggregate == "min":
            return float(min(values))
        if aggregate == "max":
            return float(max(values))
        if aggregate == "count":
            return float(len(values))
        return None

    entries_out: list[dict[str, Any]] = []
    for key, values in buckets.items():
        bucket_time, service, metric, queue_key, stream_key, group = key
        aggregated = _apply(values)
        if aggregated is None:
            continue
        entry = {"time": bucket_time.isoformat(), "value": aggregated}
        if has_service:
            entry["service"] = service
        if has_metric:
            entry["metric"] = metric
        if has_queue:
            entry["queue_key"] = queue_key
        if has_stream:
            entry["stream_key"] = stream_key
        if has_group:
            entry["group"] = group
        entries_out.append(entry)

    entries_out.sort(
        key=lambda item: (
            item.get("time", ""),
            item.get("service") or "",
            item.get("metric") or "",
            item.get("queue_key") or "",
            item.get("stream_key") or "",
        )
    )
    return entries_out


async def build_metric_series_payload(
    *, window: str, service: str | None, metric: str | None, aggregate: str
) -> Mapping[str, Any]:
    """Return the write rate payload sourced from Redis service metrics."""

    query_meta = {
        "window": window,
        "service": service,
        "metric": metric,
        "aggregate": aggregate,
    }

    normalised_aggregate = _normalise_aggregate(aggregate)
    window_delta = _parse_window(window)
    end = datetime.now(timezone.utc)
    start = end - window_delta

    try:
        raw_entries = await load_service_metrics(
            start=start, end=end, service=service, metric=metric
        )
    except (RedisError, ConnectionError) as exc:
        logger.warning("Failed to load service metrics from Redis", exc_info=True)
        return {
            "success": False,
            "error": str(exc),
            "series": [],
            "summary": {},
            "query": query_meta,
        }
    except Exception as exc:  # pragma: no cover - defensive guard
        raise HTTPException(status_code=502, detail=f"Failed to load Redis metrics: {exc}") from exc

    series = _aggregate_metric_entries(raw_entries, aggregate=normalised_aggregate)

    grouped: dict[str, dict[str, Any]] = defaultdict(lambda: {"points": 0, "sum": 0.0})
    for entry in series:
        key = entry.get("metric") or entry.get("queue_key") or entry.get("service") or "unknown"
        bucket_stats = grouped[key]
        bucket_stats["points"] += 1
        value = entry.get("value")
        if isinstance(value, (int, float)):
            bucket_stats["sum"] += float(value)

    for stats in grouped.values():
        points = stats.get("points", 0) or 1
        stats["average"] = stats["sum"] / points

    return {
        "success": True,
        "series": series,
        "summary": grouped,
        "query": query_meta,
    }


@router.get("/api/monitoring/metrics/write-rate")
async def get_metrics_write_rate(
    window: str = Query(default="1h"),
    service: str | None = Query(default=None),
    metric: str | None = Query(default=None),
    aggregate: str = Query(default="sum"),
) -> Mapping[str, Any]:
    """Query Redis for service metric write rates."""

    return await build_metric_series_payload(
        window=window, service=service, metric=metric, aggregate=aggregate
    )

@router.get("/api/monitoring/influx/write-rate", include_in_schema=False)
@router.get("/monitoring/influx/write-rate", include_in_schema=False)
async def get_metrics_write_rate_legacy(
    window: str = Query(default="1h"),
    service: str | None = Query(default=None),
    metric: str | None = Query(default=None),
    aggregate: str = Query(default="sum"),
) -> Mapping[str, Any]:
    """Backward compatible alias for legacy influx write-rate endpoint."""

    payload = await build_metric_series_payload(
        window=window, service=service, metric=metric, aggregate=aggregate
    )
    if isinstance(payload, dict):
        payload = dict(payload)
        payload.setdefault("legacy", True)
        payload.setdefault("source", payload.get("source") or "legacy-route")
        return payload
    return payload



__all__ = [
    "router",
    "build_metric_series_payload",
]
