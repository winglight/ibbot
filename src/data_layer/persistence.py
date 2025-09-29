"""Persistence backends for the unified data layer."""

from __future__ import annotations

import json
import time
from typing import Optional

from loguru import logger

from .event_bus import EventEnvelope

try:  # pragma: no cover - optional dependency
    from influxdb_client import InfluxDBClient, Point  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    InfluxDBClient = None  # type: ignore
    Point = None  # type: ignore

__all__ = [
    "PersistenceBackend",
    "NoopPersistenceBackend",
    "InfluxPersistenceBackend",
]


class PersistenceBackend:
    """Abstract interface for persisting market data events."""

    def write_event(self, envelope: EventEnvelope) -> None:
        raise NotImplementedError


class NoopPersistenceBackend(PersistenceBackend):
    """Persistence backend that simply drops events (default for tests)."""

    def write_event(self, envelope: EventEnvelope) -> None:  # pragma: no cover - trivial
        return None


class InfluxPersistenceBackend(PersistenceBackend):
    """InfluxDB-backed persistence layer for market events."""

    def __init__(
        self,
        url: str,
        *,
        bucket: str,
        org: str,
        token: Optional[str] = None,
        measurement: str = "market_events",
    ) -> None:
        if InfluxDBClient is None:
            raise RuntimeError("influxdb-client package is not installed")

        self._client = InfluxDBClient(url=url, token=token, org=org)
        self._write_api = self._client.write_api()
        buckets_api = self._client.buckets_api()

        existing_bucket = None
        try:
            response = buckets_api.find_buckets()
            bucket_entries = getattr(response, "buckets", response)
        except Exception as exc:  # pragma: no cover - network errors
            logger.warning("Failed to list InfluxDB buckets: {}", exc)
            bucket_entries = []

        for bucket_entry in bucket_entries or []:
            if getattr(bucket_entry, "name", None) == bucket:
                existing_bucket = bucket_entry
                break

        if existing_bucket is not None:
            logger.info("Found existing InfluxDB bucket {}", bucket)
        else:
            org_api = self._client.organizations_api()
            try:
                org_response = org_api.find_organizations()
            except Exception as exc:  # pragma: no cover - network errors
                raise RuntimeError("Failed to list InfluxDB organizations") from exc

            organizations = getattr(org_response, "orgs", org_response) or []
            org_id = None
            for org_entry in organizations:
                if getattr(org_entry, "name", None) == org:
                    org_id = getattr(org_entry, "id", None)
                    break

            if org_id is None:
                raise RuntimeError(f"Unable to find InfluxDB organization '{org}'")

            buckets_api.create_bucket(bucket_name=bucket, org_id=org_id)
            logger.info("Created InfluxDB bucket {}", bucket)

        self._bucket = bucket
        self._org = org
        self._measurement = measurement

    def write_event(self, envelope: EventEnvelope) -> None:
        tags = {
            "event_type": envelope.event_type,
            "topic": envelope.topic,
        }
        if envelope.symbol:
            tags["symbol"] = envelope.symbol
        if envelope.source:
            tags["source"] = envelope.source

        fields = {
            "payload": json.dumps(envelope.payload, default=str),
        }
        if envelope.meta:
            fields["meta"] = json.dumps(envelope.meta, default=str)

        timestamp_ns = envelope.ts_ns or time.time_ns()
        point = Point(self._measurement).time(timestamp_ns, write_precision="ns")
        for key, value in tags.items():
            point.tag(key, value)
        for key, value in fields.items():
            point.field(key, value)

        try:
            self._write_api.write(bucket=self._bucket, org=self._org, record=point)
        except Exception as exc:  # pragma: no cover - network errors
            logger.warning("Failed to write market event to InfluxDB: {}", exc)
