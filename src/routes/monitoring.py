"""Monitoring-centric API endpoints for dashboard views."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any, Iterable, Mapping, Sequence

from fastapi import APIRouter, HTTPException, Query

from src.ibbot.influx import get_influx_config, query_data
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
    queue_targets = _normalise_queue_targets(queues)
    stream_targets = _normalise_stream_targets(streams)

    if snapshot:
        payload = dict(snapshot)
        payload["requested_queues"] = [target.queue_key for target in queue_targets]
        payload["requested_streams"] = [
            {"stream_key": target.stream_key, "group": target.group} for target in stream_targets
        ]
        return payload

    metrics_queues = await _build_queue_metrics(redis, queue_targets)
    metrics_streams = await _build_stream_metrics(redis, stream_targets)
    return {
        "timestamp": datetime.utcnow().isoformat(),
        "queues": metrics_queues,
        "streams": metrics_streams,
        "source": "live",
    }


def _format_flux_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _prepare_flux_query(
    *,
    bucket: str,
    measurement: str,
    window: str,
    service: str | None,
    metric: str | None,
    aggregate: str,
) -> str:
    statements = [
        f'from(bucket: "{_format_flux_string(bucket)}")',
        f"  |> range(start: -{window})",
        f'  |> filter(fn: (r) => r._measurement == "{_format_flux_string(measurement)}")',
        "  |> filter(fn: (r) => exists r._field and r._field == \"value\")",
    ]
    if service:
        statements.append(f'  |> filter(fn: (r) => r.service == "{_format_flux_string(service)}")')
    if metric:
        statements.append(f'  |> filter(fn: (r) => r.metric == "{_format_flux_string(metric)}")')
    statements.extend(
        [
            f"  |> aggregateWindow(every: 1m, fn: {aggregate}, createEmpty: false)",
            "  |> keep(columns: [\"_time\", \"_value\", \"service\", \"metric\", \"queue_key\", \"stream_key\", \"group\"])",
            "  |> sort(columns: [\"_time\"], desc: false)",
        ]
    )
    return "\n".join(statements)


def _normalise_flux_table(result: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not result:
        return records
    tables = result if isinstance(result, Iterable) else [result]
    for table in tables:
        rows = getattr(table, "records", None)
        if rows is None:
            rows = table
        for record in rows or []:
            try:
                value = record.get_value() if hasattr(record, "get_value") else record.get("_value")
                ts = record.get_time() if hasattr(record, "get_time") else record.get("_time")
                values = getattr(record, "values", None)
                if values is None and isinstance(record, Mapping):
                    values = record
                entry = {
                    "time": ts.isoformat() if isinstance(ts, datetime) else str(ts),
                    "value": float(value) if value is not None else None,
                    "service": values.get("service") if values else None,
                    "metric": values.get("metric") if values else None,
                    "queue_key": values.get("queue_key") if values else None,
                    "stream_key": values.get("stream_key") if values else None,
                    "group": values.get("group") if values else None,
                }
            except Exception:  # pragma: no cover - defensive parsing
                continue
            records.append(entry)
    return records


def build_influx_write_rate_payload(
    *, window: str, service: str | None, metric: str | None, aggregate: str
) -> Mapping[str, Any]:
    """Return the write rate payload used by both HTTP and websocket handlers."""

    query_meta = {
        "window": window,
        "service": service,
        "metric": metric,
        "aggregate": aggregate,
    }

    config = get_influx_config()
    if not config.bucket:
        return {
            "success": False,
            "error": "InfluxDB bucket is not configured",
            "series": [],
            "summary": {},
            "query": query_meta,
        }

    flux = _prepare_flux_query(
        bucket=config.bucket,
        measurement="service_metrics",
        window=window,
        service=service,
        metric=metric,
        aggregate=aggregate,
    )

    try:
        result = query_data(flux, org=config.org)
    except Exception as exc:  # pragma: no cover - depends on client configuration
        raise HTTPException(status_code=502, detail=f"Failed to query InfluxDB: {exc}") from exc

    series = _normalise_flux_table(result)
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


@router.get("/api/monitoring/influx/write-rate")
async def get_influx_write_rate(
    window: str = Query(default="1h"),
    service: str | None = Query(default=None),
    metric: str | None = Query(default=None),
    aggregate: str = Query(default="sum"),
) -> Mapping[str, Any]:
    """Query InfluxDB for service metric write rates."""

    return build_influx_write_rate_payload(
        window=window, service=service, metric=metric, aggregate=aggregate
    )


__all__ = [
    "router",
    "build_influx_write_rate_payload",
]

