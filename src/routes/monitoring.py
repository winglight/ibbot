"""Monitoring-centric API endpoints for dashboard views."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any, Callable, Iterable, Mapping, Sequence

import asyncio
import re
from collections import defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Query

from src.db import (
    MariaDBConfigurationError,
    get_service_metrics_columns,
    run_sql_query,
)
from src.ibbot.redis import (
    DEFAULT_COMMAND_QUEUE,
    DEFAULT_MARKET_STREAM,
    DEFAULT_STREAM_GROUP,
    get_queue_length,
    get_redis_client,
    get_stream_metrics,
)

router = APIRouter()


QUEUE_MONITOR_HASH_KEY = "hash:service:queue-monitor"


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
                cursor, data = await hscan(key, cursor=cursor, count=max(limit * 2, 10))
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
                data = await hgetall(key)
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
    except Exception as exc:  # pragma: no cover - depends on redis client implementation
        error = str(exc)
    return entries[:limit], has_more, error


async def _collect_scan_keys(redis, *, pattern: str, limit: int = 5) -> tuple[list[str], bool, str | None]:
    keys: list[str] = []
    has_more = False
    error: str | None = None
    scan_iter = getattr(redis, "scan_iter", None)
    try:
        if callable(scan_iter):
            async for raw in scan_iter(match=pattern, count=max(limit * 5, 20)):
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
                    cursor, batch = await scan(cursor=cursor, match=pattern, count=max(limit * 5, 20))
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
    except Exception as exc:  # pragma: no cover - depends on redis client implementation
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


async def _collect_data_fields(redis) -> dict[str, Any]:
    if redis is None:
        return {
            "configs": {"samples": [], "has_more": False, "count": 0, "error": "Redis unavailable"},
            "orders": {"samples": [], "has_more": False, "count": 0, "error": "Redis unavailable"},
            "subscriptions": {"samples": [], "has_more": False, "count": 0, "error": "Redis unavailable"},
        }

    def _config_label(key: str) -> str:
        return key.split(":", 2)[-1] if ":" in key else key

    def _order_label(key: str) -> str:
        return key.split(":", 2)[-1] if ":" in key else key

    def _subscription_label(key: str) -> str:
        return key.split(":", 2)[-1] if ":" in key else key

    configs_task = _summarise_hash_category(
        redis,
        pattern="hash:config:*",
        label_fn=_config_label,
    )
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
        if isinstance(result, Exception):  # pragma: no cover - defensive guard
            summary[key]["error"] = str(result)
            continue
        summary[key] = result
    return summary


async def _load_snapshot_from_redis(redis) -> dict[str, Any] | None:
    try:
        payload = await redis.hgetall(QUEUE_MONITOR_HASH_KEY)
    except Exception as exc:  # pragma: no cover - redis connection issues
        raise HTTPException(status_code=503, detail=f"Redis error: {exc}") from exc

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
            length = await get_queue_length(queue_key=target.queue_key, redis=redis)
        except Exception as exc:  # pragma: no cover - redis connection issues
            raise HTTPException(status_code=503, detail=f"Redis error: {exc}") from exc
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
            metrics = await get_stream_metrics(
                stream_key=target.stream_key,
                group=target.group,
                redis=redis,
            )
        except Exception as exc:  # pragma: no cover - redis connection issues
            raise HTTPException(status_code=503, detail=f"Redis error: {exc}") from exc
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


@router.get("/api/monitoring/queues")
async def get_queue_metrics(
    queues: Sequence[str] | None = Query(default=None, alias="queue"),
    streams: Sequence[str] | None = Query(default=None, alias="stream"),
) -> Mapping[str, Any]:
    """Return queue and stream metrics for dashboard consumption."""

    redis = await get_redis_client()

    snapshot = await _load_snapshot_from_redis(redis)
    data_fields = await _collect_data_fields(redis)
    queue_targets = _normalise_queue_targets(queues)
    stream_targets = _normalise_stream_targets(streams)

    if snapshot:
        payload = dict(snapshot)
        payload["requested_queues"] = [target.queue_key for target in queue_targets]
        payload["requested_streams"] = [
            {"stream_key": target.stream_key, "group": target.group} for target in stream_targets
        ]
        payload["data_fields"] = data_fields
        return payload

    metrics_queues = await _build_queue_metrics(redis, queue_targets)
    metrics_streams = await _build_stream_metrics(redis, stream_targets)
    return {
        "timestamp": datetime.utcnow().isoformat(),
        "queues": metrics_queues,
        "streams": metrics_streams,
        "source": "live",
        "data_fields": data_fields,
    }


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


def _aggregate_metric_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    aggregate: str,
    has_service: bool,
    has_metric: bool,
    has_queue: bool,
    has_stream: bool,
    has_group: bool,
) -> list[dict[str, Any]]:
    buckets: dict[tuple[Any, ...], list[float]] = defaultdict(list)

    for row in rows:
        timestamp_value = row.get("bucket_time")
        bucket_time = _coerce_datetime(timestamp_value)
        if bucket_time is None:
            continue
        bucket_time = bucket_time.replace(second=0, microsecond=0)

        raw_value = row.get("metric_value")
        try:
            numeric = float(raw_value)
        except (TypeError, ValueError):
            continue

        meta: dict[str, Any] = {}
        if has_service:
            meta["service"] = row.get("service")
        if has_metric:
            meta["metric"] = row.get("metric")
        if has_queue:
            meta["queue_key"] = row.get("queue_key")
        if has_stream:
            meta["stream_key"] = row.get("stream_key")
        if has_group:
            meta["group"] = row.get("group_name")

        key = (
            bucket_time,
            meta.get("service"),
            meta.get("metric"),
            meta.get("queue_key"),
            meta.get("stream_key"),
            meta.get("group"),
        )
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

    entries: list[dict[str, Any]] = []
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
        entries.append(entry)

    entries.sort(
        key=lambda item: (
            item.get("time", ""),
            item.get("service") or "",
            item.get("metric") or "",
            item.get("queue_key") or "",
            item.get("stream_key") or "",
        )
    )
    return entries


def build_metric_series_payload(
    *, window: str, service: str | None, metric: str | None, aggregate: str
) -> Mapping[str, Any]:
    """Return the write rate payload used by both HTTP and websocket handlers."""

    query_meta = {
        "window": window,
        "service": service,
        "metric": metric,
        "aggregate": aggregate,
    }

    try:
        columns = get_service_metrics_columns()
    except MariaDBConfigurationError as exc:
        return {
            "success": False,
            "error": str(exc),
            "series": [],
            "summary": {},
            "query": query_meta,
        }

    def _resolve(*candidates: str) -> str | None:
        for candidate in candidates:
            if candidate in columns:
                return candidate
        return None

    timestamp_col = _resolve("recorded_at", "timestamp", "created_at")
    value_col = _resolve("value", "metric_value", "count")
    service_col = _resolve("service", "service_name")
    metric_col = _resolve("metric", "metric_name")
    queue_col = _resolve("queue_key", "queue")
    stream_col = _resolve("stream_key", "stream")
    group_col = _resolve("group_name", "group")

    if not timestamp_col or not value_col:
        return {
            "success": False,
            "error": "service_metrics table is missing timestamp or value columns",
            "series": [],
            "summary": {},
            "query": query_meta,
        }

    def _column(name: str) -> str:
        return f"`{name}`"

    select_parts = [
        f"{_column(timestamp_col)} AS bucket_time",
        f"{_column(value_col)} AS metric_value",
    ]
    if service_col:
        select_parts.append(f"{_column(service_col)} AS service")
    if metric_col:
        select_parts.append(f"{_column(metric_col)} AS metric")
    if queue_col:
        select_parts.append(f"{_column(queue_col)} AS queue_key")
    if stream_col:
        select_parts.append(f"{_column(stream_col)} AS stream_key")
    if group_col:
        select_parts.append(f"{_column(group_col)} AS group_name")

    where_clauses = [f"{_column(timestamp_col)} >= :start"]
    params: dict[str, Any] = {
        "start": datetime.utcnow() - _parse_window(window),
    }

    if service:
        if not service_col:
            raise HTTPException(status_code=400, detail="service filter is not supported")
        where_clauses.append(f"{_column(service_col)} = :service")
        params["service"] = service

    if metric:
        if not metric_col:
            raise HTTPException(status_code=400, detail="metric filter is not supported")
        where_clauses.append(f"{_column(metric_col)} = :metric")
        params["metric"] = metric

    sql = " ".join(
        [
            "SELECT",
            ", ".join(select_parts),
            "FROM service_metrics",
            "WHERE",
            " AND ".join(where_clauses),
            f"ORDER BY {_column(timestamp_col)} ASC",
        ]
    )

    try:
        rows = run_sql_query(sql, params)
    except MariaDBConfigurationError as exc:
        return {
            "success": False,
            "error": str(exc),
            "series": [],
            "summary": {},
            "query": query_meta,
        }
    except Exception as exc:  # pragma: no cover - depends on DB driver
        raise HTTPException(status_code=502, detail=f"Failed to query MariaDB: {exc}") from exc

    series = _aggregate_metric_rows(
        rows,
        aggregate=_normalise_aggregate(aggregate),
        has_service=service_col is not None,
        has_metric=metric_col is not None,
        has_queue=queue_col is not None,
        has_stream=stream_col is not None,
        has_group=group_col is not None,
    )

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
    """Query MariaDB for service metric write rates."""

    return build_metric_series_payload(
        window=window, service=service, metric=metric, aggregate=aggregate
    )


__all__ = [
    "router",
    "build_metric_series_payload",
]

