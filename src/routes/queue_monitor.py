"""Helper client for loading queue monitor snapshots from Redis.

This module provides :class:`QueueMonitorClient` which fetches the snapshot
stored by the ``queue-monitor`` service in Redis. The client normalises the
payload and augments it with live queue lengths to offer a consistent response
for the API routes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Mapping

from src.ibbot.redis import get_queue_length, get_redis_client, read_hash


JsonMapping = Mapping[str, Any]


def _to_int(value: Any) -> int | None:
    """Best effort conversion of ``value`` to :class:`int`."""

    if value is None:
        return None
    if isinstance(value, bool):  # guard against bool being a subclass of int
        return int(value)
    if isinstance(value, (int, float)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None
    return None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


def _parse_snapshot_entry(raw: Any) -> dict[str, Any] | None:
    """Parse an entry from the queue monitor hash into a mapping."""

    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None
        if isinstance(parsed, Mapping):
            return dict(parsed)
    return None


@dataclass(slots=True)
class QueueMonitorClient:
    """Load and normalise queue monitor snapshots from Redis."""

    redis_factory: Callable[[], Awaitable[Any]] = get_redis_client
    hash_key: str = "hash:service:queue-monitor"

    async def fetch_snapshot(self) -> dict[str, Any] | None:
        """Return the latest snapshot with live queue lengths.

        The response contains ``timestamp``, ``queues`` and ``streams`` keys.  If
        the Redis hash is missing or empty ``None`` is returned.
        """

        redis = await self.redis_factory()
        payload = await read_hash(self.hash_key, redis=redis)
        if not payload:
            return None

        timestamp = str(payload.get("timestamp") or "") or None
        queues = await self._build_queue_entries(payload.items(), redis)
        streams = self._build_stream_entries(payload.items())
        totals = {
            "queue_length": sum(entry.get("length", 0) or 0 for entry in queues),
            "stream_pending": sum(entry.get("pending", 0) or 0 for entry in streams),
        }
        return {
            "timestamp": timestamp,
            "queues": queues,
            "streams": streams,
            "totals": totals,
        }

    async def _build_queue_entries(
        self, items: Iterable[tuple[str, Any]], redis: Any
    ) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for key, raw in items:
            if not key.startswith("queue:"):
                continue
            details = _parse_snapshot_entry(raw)
            if not details:
                continue
            queue_key = str(details.get("queue_key") or "")
            queue_name = str(details.get("name") or "") or key.split(":", 1)[-1]
            snapshot_length = _to_int(details.get("length"))
            actual_length: int | None = None
            if queue_key:
                try:
                    actual_length = await get_queue_length(queue_key=queue_key, redis=redis)
                except Exception:
                    actual_length = None
            length = actual_length
            length_source = "redis"
            if length is None:
                length = snapshot_length or 0
                length_source = "snapshot" if snapshot_length is not None else "unknown"
            entry = {
                "name": queue_name,
                "queue_key": queue_key or None,
                "length": int(length),
                "length_source": length_source,
                "snapshot_length": snapshot_length,
                "consumption_rate": _to_float(details.get("consumption_rate")),
                "stale_seconds": _to_float(details.get("stale_seconds")),
                "metrics": details,
            }
            entries.append(entry)
        return entries

    def _build_stream_entries(self, items: Iterable[tuple[str, Any]]) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for key, raw in items:
            if not key.startswith("stream:"):
                continue
            details = _parse_snapshot_entry(raw)
            if not details:
                continue
            stream_key = str(details.get("stream_key") or "")
            stream_name = str(details.get("name") or "") or key.split(":", 1)[-1]
            group = str(details.get("group") or "") or None
            entry = {
                "name": stream_name,
                "stream_key": stream_key or None,
                "group": group,
                "pending": _to_int(details.get("pending")) or 0,
                "consumption_rate": _to_float(details.get("consumption_rate")),
                "stale_seconds": _to_float(details.get("stale_seconds")),
                "metrics": details,
            }
            entries.append(entry)
        return entries

    @staticmethod
    def find_queue(snapshot: Mapping[str, Any], identifier: str) -> Mapping[str, Any] | None:
        """Locate a queue entry by ``identifier`` (name or queue key)."""

        lookup = identifier.lower()
        for entry in snapshot.get("queues", []):
            name = entry.get("name")
            queue_key = entry.get("queue_key")
            candidates = [name, queue_key]
            for candidate in candidates:
                if candidate and str(candidate).lower() == lookup:
                    return entry
        return None

