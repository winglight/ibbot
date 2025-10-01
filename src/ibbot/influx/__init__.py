"""InfluxDB utilities for ibbot.

This module provides a light-weight abstraction around the official
``influxdb-client`` package. The helpers here hide the slightly verbose
initialisation ceremony while offering builders for the measurement types used
throughout the project (orders, quotes, DOM snapshots and service metrics).

The implementation is intentionally defensive so that most functionality can be
unit tested without the real client dependency – the public APIs accept injected
client objects and fall back to environment driven configuration otherwise.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping, MutableMapping

try:  # Optional dependency during testing.
    from influxdb_client import InfluxDBClient
    from influxdb_client.client.write_api import SYNCHRONOUS
except Exception:  # pragma: no cover - the dependency is optional
    InfluxDBClient = None  # type: ignore[assignment]
    SYNCHRONOUS = object()

DEFAULT_INFLUX_URL_ENV = "IBBOT_INFLUX_URL"
DEFAULT_INFLUX_TOKEN_ENV = "IBBOT_INFLUX_TOKEN"
DEFAULT_INFLUX_ORG_ENV = "IBBOT_INFLUX_ORG"
DEFAULT_INFLUX_BUCKET_ENV = "IBBOT_INFLUX_BUCKET"
# Secondary fallbacks used by the data layer bootstrap scripts. These allow the
# UI application to seamlessly reuse the same configuration without requiring
# duplicated environment variables.
FALLBACK_INFLUX_URL_ENV = "DATA_LAYER_INFLUX_URL"
FALLBACK_INFLUX_TOKEN_ENV = "DATA_LAYER_INFLUX_TOKEN"
FALLBACK_INFLUX_ORG_ENV = "DATA_LAYER_INFLUX_ORG"
FALLBACK_INFLUX_BUCKET_ENV = "DATA_LAYER_INFLUX_BUCKET"
DEFAULT_INFLUX_URL = "http://localhost:8086"
DEFAULT_TIMEOUT = 10_000

_client_lock = threading.Lock()
_client: Any | None = None
_write_api_lock = threading.Lock()
_write_api: Any | None = None


@dataclass(slots=True)
class InfluxConfig:
    """Container for the minimal configuration required by the client."""

    url: str
    token: str | None = None
    org: str | None = None
    bucket: str | None = None

    @classmethod
    def from_env(
        cls,
        *,
        url: str | None = None,
        token: str | None = None,
        org: str | None = None,
        bucket: str | None = None,
    ) -> "InfluxConfig":
        resolved_url = url or os.getenv(DEFAULT_INFLUX_URL_ENV)
        if not resolved_url:
            resolved_url = os.getenv(FALLBACK_INFLUX_URL_ENV, DEFAULT_INFLUX_URL)

        resolved_token = (
            token
            if token is not None
            else os.getenv(DEFAULT_INFLUX_TOKEN_ENV)
            or os.getenv(FALLBACK_INFLUX_TOKEN_ENV)
        )

        resolved_org = (
            org
            if org is not None
            else os.getenv(DEFAULT_INFLUX_ORG_ENV)
            or os.getenv(FALLBACK_INFLUX_ORG_ENV)
        )

        resolved_bucket = (
            bucket
            if bucket is not None
            else os.getenv(DEFAULT_INFLUX_BUCKET_ENV)
            or os.getenv(FALLBACK_INFLUX_BUCKET_ENV)
        )

        return cls(resolved_url, resolved_token, resolved_org, resolved_bucket)


_config: InfluxConfig | None = None


@dataclass(slots=True)
class InfluxPoint:
    """Structured representation of a measurement that can be serialised."""

    measurement: str
    fields: Mapping[str, Any]
    tags: Mapping[str, str] | None = None
    timestamp: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"measurement": self.measurement, "fields": dict(self.fields)}
        if self.tags:
            payload["tags"] = dict(self.tags)
        if self.timestamp is not None:
            payload["time"] = self.timestamp
        return payload


@dataclass(slots=True)
class WriteStatistics:
    """Collects counters about write invocations for observability."""

    batches: int = 0
    points: int = 0
    total_duration: float = 0.0
    last_error: str | None = None
    last_write_at: float | None = None

    @property
    def points_per_second(self) -> float:
        if self.total_duration <= 0:
            return 0.0
        return self.points / self.total_duration


_stats_lock = threading.Lock()
_stats = WriteStatistics()


def _ensure_client_available() -> None:
    if InfluxDBClient is None:  # pragma: no cover - only triggered without dependency
        raise RuntimeError(
            "influxdb-client is required but not installed. Install influxdb-client to use this module."
        )


def configure_influx(**kwargs: Any) -> InfluxConfig:
    """Update the cached configuration using explicit values or environment fallbacks."""

    global _config
    _config = InfluxConfig.from_env(**kwargs)
    return _config


def get_influx_config() -> InfluxConfig:
    """Return the active configuration, initialising from environment on first call."""

    global _config
    if _config is None:
        _config = InfluxConfig.from_env()
    return _config


def init_influx_client(**kwargs: Any) -> Any:
    """Initialise and cache a new InfluxDB client using the provided overrides."""

    return get_influx_client(force_refresh=True, **kwargs)


def get_influx_client(*, force_refresh: bool = False, **kwargs: Any) -> Any:
    """Return a cached InfluxDB client, creating it on demand."""

    global _client
    with _client_lock:
        if force_refresh or _client is None:
            _ensure_client_available()
            config = configure_influx(**kwargs)
            _client = InfluxDBClient(  # type: ignore[operator]
                url=config.url,
                token=config.token,
                org=config.org,
                timeout=DEFAULT_TIMEOUT,
            )
    return _client


def close_influx_client() -> None:
    """Dispose of the cached client and write API (primarily used by tests)."""

    global _client, _write_api
    with _client_lock:
        client = _client
        _client = None
    with _write_api_lock:
        _write_api = None
    if client is not None:
        try:  # pragma: no cover - depends on client implementation
            client.close()
        except Exception:
            pass


def _ensure_write_api(client: Any | None = None) -> Any:
    if client is not None:
        return client.write_api(write_options=SYNCHRONOUS)
    global _write_api
    if _write_api is None:
        with _write_api_lock:
            if _write_api is None:
                resolved_client = get_influx_client()
                _write_api = resolved_client.write_api(write_options=SYNCHRONOUS)
    return _write_api


def _normalise_points(points: Iterable[InfluxPoint | Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    normalised: list[Mapping[str, Any]] = []
    for point in points:
        if isinstance(point, InfluxPoint):
            normalised.append(point.as_dict())
        else:
            normalised.append(dict(point))
    return normalised


def write_points(
    points: Iterable[InfluxPoint | Mapping[str, Any]],
    *,
    bucket: str | None = None,
    org: str | None = None,
    client: Any | None = None,
    write_precision: str = "ns",
) -> int:
    """Write a batch of points to InfluxDB and update statistics."""

    batch = _normalise_points(points)
    if not batch:
        return 0

    config = get_influx_config()
    bucket_name = bucket or config.bucket
    if not bucket_name:
        raise ValueError("A target bucket must be provided either explicitly or via configuration")
    org_name = org or config.org

    write_api = _ensure_write_api(client)
    start = time.perf_counter()
    try:
        write_api.write(bucket=bucket_name, org=org_name, record=batch, write_precision=write_precision)
        _record_write(len(batch), time.perf_counter() - start)
    except Exception as exc:  # pragma: no cover - depends on client behaviour
        _record_write(len(batch), time.perf_counter() - start, error=str(exc))
        raise
    return len(batch)


def _record_write(points: int, duration: float, *, error: str | None = None) -> None:
    with _stats_lock:
        global _stats
        stats = _stats
        stats.batches += 1
        stats.points += points
        stats.total_duration += duration
        stats.last_error = error
        stats.last_write_at = time.time()


def get_write_statistics() -> WriteStatistics:
    """Return a snapshot of the accumulated write statistics."""

    with _stats_lock:
        stats = _stats
        return WriteStatistics(
            batches=stats.batches,
            points=stats.points,
            total_duration=stats.total_duration,
            last_error=stats.last_error,
            last_write_at=stats.last_write_at,
        )


def reset_write_statistics() -> None:
    """Reset the write statistics counters."""

    global _stats
    with _stats_lock:
        _stats = WriteStatistics()


def query_data(query: str, *, org: str | None = None, client: Any | None = None) -> Any:
    """Execute an Influx query and return the result."""

    config = get_influx_config()
    query_api = (client or get_influx_client()).query_api()
    return query_api.query(org=org or config.org, query=query)


def ensure_retention_policy(
    bucket: str,
    *,
    retention_hours: int | None = None,
    org: str | None = None,
    client: Any | None = None,
) -> None:
    """Create or update a bucket's retention policy."""

    resolved_client = client or get_influx_client()
    buckets_api = resolved_client.buckets_api()
    existing = buckets_api.find_bucket_by_name(bucket)
    retention_seconds = int(retention_hours * 3600) if retention_hours else None

    if existing is None:
        payload: MutableMapping[str, Any] = {"bucket_name": bucket}
        resolved_org = org or get_influx_config().org
        if resolved_org:
            payload["org"] = resolved_org
        if retention_seconds is not None:
            payload["retention_rules"] = [{"every_seconds": retention_seconds}]
        buckets_api.create_bucket(**payload)
        return

    if retention_seconds is None:
        return

    current_rules = getattr(existing, "retention_rules", None)
    current_seconds: int | None = None
    if current_rules:
        rule = current_rules[0]
        if isinstance(rule, Mapping):
            current_seconds = int(rule.get("every_seconds", 0))
        else:
            current_seconds = int(getattr(rule, "every_seconds", 0))
    if current_seconds == retention_seconds:
        return

    new_rules = [{"every_seconds": retention_seconds}]
    if isinstance(existing, MutableMapping):
        existing["retention_rules"] = new_rules
    else:
        setattr(existing, "retention_rules", new_rules)
    buckets_api.update_bucket(existing)


def build_order_point(
    *,
    order_id: str,
    symbol: str,
    side: str,
    quantity: float,
    price: float,
    status: str,
    exchange: str | None = None,
    timestamp: datetime | None = None,
    extra_fields: Mapping[str, Any] | None = None,
    extra_tags: Mapping[str, str] | None = None,
) -> InfluxPoint:
    fields: dict[str, Any] = {
        "price": float(price),
        "quantity": float(quantity),
        "status": status,
    }
    if extra_fields:
        fields.update(extra_fields)
    tags: dict[str, str] = {"order_id": order_id, "symbol": symbol, "side": side}
    if exchange:
        tags["exchange"] = exchange
    if extra_tags:
        tags.update(extra_tags)
    return InfluxPoint("orders", fields=fields, tags=tags, timestamp=timestamp)


def build_quote_point(
    *,
    symbol: str,
    bid: float,
    ask: float,
    bid_size: float,
    ask_size: float,
    source: str | None = None,
    timestamp: datetime | None = None,
    extra_fields: Mapping[str, Any] | None = None,
    extra_tags: Mapping[str, str] | None = None,
) -> InfluxPoint:
    fields: dict[str, Any] = {
        "bid": float(bid),
        "ask": float(ask),
        "bid_size": float(bid_size),
        "ask_size": float(ask_size),
    }
    if extra_fields:
        fields.update(extra_fields)
    tags: dict[str, str] = {"symbol": symbol}
    if source:
        tags["source"] = source
    if extra_tags:
        tags.update(extra_tags)
    return InfluxPoint("quotes", fields=fields, tags=tags, timestamp=timestamp)


def build_dom_point(
    *,
    symbol: str,
    level: int,
    side: str,
    price: float,
    size: float,
    timestamp: datetime | None = None,
    extra_fields: Mapping[str, Any] | None = None,
    extra_tags: Mapping[str, str] | None = None,
) -> InfluxPoint:
    fields: dict[str, Any] = {"price": float(price), "size": float(size)}
    if extra_fields:
        fields.update(extra_fields)
    tags: dict[str, str] = {"symbol": symbol, "level": str(level), "side": side}
    if extra_tags:
        tags.update(extra_tags)
    return InfluxPoint("dom", fields=fields, tags=tags, timestamp=timestamp)


def build_service_metric_point(
    *,
    service: str,
    metric: str,
    value: float,
    host: str | None = None,
    status: str | None = None,
    timestamp: datetime | None = None,
    extra_fields: Mapping[str, Any] | None = None,
    extra_tags: Mapping[str, str] | None = None,
) -> InfluxPoint:
    fields: dict[str, Any] = {"value": float(value)}
    if status is not None:
        fields["status"] = status
    if extra_fields:
        fields.update(extra_fields)
    tags: dict[str, str] = {"service": service, "metric": metric}
    if host:
        tags["host"] = host
    if extra_tags:
        tags.update(extra_tags)
    return InfluxPoint("service_metrics", fields=fields, tags=tags, timestamp=timestamp)


def reset_influx_state() -> None:
    """Convenience helper for tests to reset cached state."""

    close_influx_client()
    reset_write_statistics()
    global _config
    _config = None


__all__ = [
    "InfluxConfig",
    "InfluxPoint",
    "WriteStatistics",
    "build_dom_point",
    "build_order_point",
    "build_quote_point",
    "build_service_metric_point",
    "configure_influx",
    "ensure_retention_policy",
    "get_influx_client",
    "get_influx_config",
    "get_write_statistics",
    "init_influx_client",
    "query_data",
    "reset_influx_state",
    "reset_write_statistics",
    "write_points",
]
