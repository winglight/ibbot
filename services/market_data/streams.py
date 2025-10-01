"""Redis stream publisher for market data updates."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable, Mapping

from loguru import logger

__all__ = ["StreamPublisher"]


ErrorCallback = Callable[[str | None], None]


class StreamPublisher:
    """Write normalised market data events into a Redis stream."""

    def __init__(
        self,
        *,
        redis_client: Any,
        stream_key: str = "stream:market-service",
        maxlen: int = 5000,
        approximate: bool = True,
        error_callback: ErrorCallback | None = None,
    ) -> None:
        if maxlen <= 0:
            raise ValueError("maxlen must be a positive integer")

        self._redis = redis_client
        self.stream_key = stream_key
        self.maxlen = maxlen
        self.approximate = approximate
        self._error_callback = error_callback

        self._lock = asyncio.Lock()
        self._event_count = 0
        self._latency_samples = 0
        self._latency_total = 0.0
        self._latency_max = 0.0
        self._last_publish_ns: int | None = None
        self._last_error: str | None = None

    @property
    def last_error(self) -> str | None:
        """Return the most recent stream publish error, if any."""

        return self._last_error

    async def publish(
        self,
        *,
        symbol: str,
        event_type: str,
        data: Mapping[str, Any] | Any,
        ts_ns: int | None = None,
        latency_ms: float | None = None,
    ) -> bool:
        """Append an event to the Redis stream."""

        if self._redis is None:
            logger.debug("Redis client unavailable, skipping stream publish")
            return False

        timestamp_ns = ts_ns or time.time_ns()
        payload = self._normalise_payload(data)
        entry = {
            "symbol": symbol,
            "type": event_type,
            "data": json.dumps(payload, default=str),
            "ts_ns": str(timestamp_ns),
            "latency_ms": "" if latency_ms is None else str(float(latency_ms)),
        }

        try:
            await self._redis.xadd(
                self.stream_key,
                entry,
                maxlen=self.maxlen,
                approximate=self.approximate,
            )
        except Exception as exc:  # pragma: no cover - defensive logging
            message = f"Failed to publish stream event: {exc}"
            logger.warning(message)
            self._last_error = message
            if self._error_callback is not None:
                self._error_callback(message)
            return False

        async with self._lock:
            self._event_count += 1
            if latency_ms is not None:
                self._latency_samples += 1
                self._latency_total += float(latency_ms)
                if float(latency_ms) > self._latency_max:
                    self._latency_max = float(latency_ms)
            self._last_publish_ns = timestamp_ns

        if self._last_error is not None:
            # Clear the cached error on successful write for health reporting.
            self._last_error = None
            if self._error_callback is not None:
                self._error_callback(None)

        return True

    async def collect_metrics(self) -> dict[str, Any]:
        """Return and reset stream statistics for the current interval."""

        async with self._lock:
            event_count = self._event_count
            latency_samples = self._latency_samples
            latency_total = self._latency_total
            latency_max = self._latency_max if latency_samples else None
            last_publish_ns = self._last_publish_ns

            self._event_count = 0
            self._latency_samples = 0
            self._latency_total = 0.0
            self._latency_max = 0.0

        avg_latency = None
        if latency_samples:
            avg_latency = latency_total / latency_samples

        return {
            "events": event_count,
            "avg_latency_ms": avg_latency,
            "max_latency_ms": latency_max,
            "latency_samples": latency_samples,
            "last_publish_ts_ns": last_publish_ns,
            "last_error": self._last_error,
        }

    async def get_summary(self) -> dict[str, Any]:
        """Return the current stream statistics without resetting counters."""

        async with self._lock:
            latency_samples = self._latency_samples
            latency_total = self._latency_total
            avg_latency = None
            if latency_samples:
                avg_latency = latency_total / latency_samples

            return {
                "events": self._event_count,
                "avg_latency_ms": avg_latency,
                "max_latency_ms": self._latency_max if latency_samples else None,
                "latency_samples": latency_samples,
                "last_publish_ts_ns": self._last_publish_ns,
                "last_error": self._last_error,
            }

    def _normalise_payload(self, data: Mapping[str, Any] | Any) -> Mapping[str, Any]:
        if isinstance(data, Mapping):
            return data
        return {"value": data}
