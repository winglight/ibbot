"""Runtime management for the market data service."""

from __future__ import annotations

import asyncio
import inspect
import os
import time
from copy import deepcopy
from contextlib import suppress
from typing import Any, Awaitable, Callable, Protocol

from loguru import logger

from src.cache import ensure_redis
from src.configuration.provider import (
    ConfigProvider,
    MarketDataServiceConfig,
    get_provider,
)
from src.data_layer.event_bus import EventEnvelope
from src.data_layer.persistence import InfluxPersistenceBackend, PersistenceBackend

from .streams import StreamPublisher
from .subscriptions import PubSubListener


class SupportsLifecycle(Protocol):
    """Simple protocol describing listener objects with start/stop hooks."""

    async def start(self) -> None:
        ...

    async def stop(self) -> None:
        ...


ListenerFactory = Callable[[MarketDataServiceConfig, "MarketDataRuntime"], Awaitable[SupportsLifecycle] | SupportsLifecycle]
MetricsFactory = Callable[["MarketDataRuntime"], Awaitable[Any]]
MetricsBackendFactory = Callable[[MarketDataServiceConfig], Awaitable[PersistenceBackend | None] | PersistenceBackend | None]
ConfigProviderFactory = Callable[[], Awaitable[ConfigProvider[MarketDataServiceConfig]]]
RedisFactory = Callable[[], Awaitable[Any]]


class MarketDataRuntime:
    """Coordinate the long running tasks for the market data service."""

    def __init__(
        self,
        *,
        config_provider_factory: ConfigProviderFactory | None = None,
        redis_factory: RedisFactory = ensure_redis,
        listener_factory: ListenerFactory | None = None,
        metrics_factory: MetricsFactory | None = None,
        metrics_backend_factory: MetricsBackendFactory | None = None,
        service_name: str | None = None,
        stream_key: str | None = None,
        metrics_period: float | None = None,
    ) -> None:
        self._config_provider_factory = config_provider_factory or get_provider
        self._redis_factory = redis_factory
        self._listener_factory = listener_factory
        self._metrics_factory = metrics_factory
        self._metrics_backend_factory = metrics_backend_factory
        self._lock = asyncio.Lock()
        self._shutdown_event = asyncio.Event()

        self.config_provider: ConfigProvider[MarketDataServiceConfig] | None = None
        self.config: MarketDataServiceConfig | None = None
        self.redis: Any = None
        self.listener: SupportsLifecycle | None = None
        self.metrics_task: asyncio.Task[Any] | None = None
        self.metrics_backend: PersistenceBackend | None = None
        self.stream_publisher: StreamPublisher | None = None
        self._snapshots: dict[str, dict[str, Any]] = {}

        self.service_name = service_name
        self.stream_key = stream_key
        self.metrics_period = metrics_period
        self._started = False
        self.health_state: dict[str, Any] = {
            "last_error": None,
            "last_stream_error": None,
            "last_metrics_error": None,
        }

    async def startup(self) -> None:
        """Initialise dependencies and start background tasks."""

        async with self._lock:
            if self._started:
                raise RuntimeError("MarketDataRuntime has already been started")

            provider = self.config_provider
            if provider is None:
                provider = await self._config_provider_factory()
                self.config_provider = provider

            await provider.start()
            config = await provider.get_config()
            self.config = config

            if self.service_name is None:
                self.service_name = config.service_name
            if self.stream_key is None:
                self.stream_key = config.stream_key
            if self.metrics_period is None:
                self.metrics_period = float(config.metrics_period)

            self.redis = await self._redis_factory()
            self._shutdown_event.clear()

            listener: SupportsLifecycle | None
            if self._listener_factory is not None:
                listener_obj = self._listener_factory(config, self)
                listener = await listener_obj if inspect.isawaitable(listener_obj) else listener_obj
            else:
                assert provider is not None
                listener = PubSubListener(
                    config_provider=provider,
                    redis_client=self.redis,
                    runtime=self,
                )
            if listener is not None:
                self.listener = listener
                await listener.start()

            if self.redis is not None:
                maxlen = getattr(config, "stream_length", 5000)
                self.stream_publisher = StreamPublisher(
                    redis_client=self.redis,
                    stream_key=self.stream_key,
                    maxlen=maxlen,
                    error_callback=self._record_stream_error,
                )

            if self._metrics_factory is not None:
                metrics_coro = self._metrics_factory(self)
                if not inspect.isawaitable(metrics_coro):
                    raise TypeError("metrics_factory must return an awaitable")
                self.metrics_task = asyncio.create_task(metrics_coro)
            elif self.metrics_period is not None:
                self.metrics_backend = await self._resolve_metrics_backend(config)
                self.metrics_task = asyncio.create_task(self._default_metrics_loop())

            self._started = True
            logger.info(
                "Market data runtime started",
                service=self.service_name,
                stream_key=self.stream_key,
            )

    async def shutdown(self) -> None:
        """Stop all background tasks and release resources."""

        async with self._lock:
            if not self._started:
                return

            self._shutdown_event.set()

            if self.metrics_task is not None:
                self.metrics_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self.metrics_task
                self.metrics_task = None

            if self.listener is not None:
                with suppress(Exception):
                    await self.listener.stop()
                self.listener = None

            if self.config_provider is not None:
                with suppress(Exception):
                    await self.config_provider.stop()

            self.stream_publisher = None
            self.metrics_backend = None
            self.redis = None
            self._started = False
            logger.info("Market data runtime stopped", service=self.service_name)

    async def publish_stream_event(
        self,
        *,
        symbol: str,
        event_type: str,
        data: Any,
        message_timestamp: Any | None,
        latency_ms: float | None,
    ) -> None:
        """Publish a normalised event to the Redis stream if available."""

        publisher = self.stream_publisher
        if publisher is None:
            return

        ts_ns = None
        if message_timestamp is not None:
            try:
                if hasattr(message_timestamp, "timestamp"):
                    ts_ns = int(message_timestamp.timestamp() * 1_000_000_000)
                else:
                    ts_ns = int(float(message_timestamp) * 1_000_000_000)
            except (TypeError, ValueError, OverflowError):
                ts_ns = None

        await publisher.publish(
            symbol=symbol,
            event_type=event_type,
            data=data,
            ts_ns=ts_ns,
            latency_ms=latency_ms,
        )

    async def _default_metrics_loop(self) -> None:
        """Periodically emit service metrics."""

        assert self.metrics_period is not None
        try:
            while not self._shutdown_event.is_set():
                await asyncio.sleep(self.metrics_period)
                await self._emit_metrics()
        except asyncio.CancelledError:
            raise

    async def _emit_metrics(self) -> None:
        publisher = self.stream_publisher
        backend = self.metrics_backend
        if publisher is None or backend is None:
            return

        metrics = await publisher.collect_metrics()
        if not metrics["events"] and not metrics["latency_samples"] and not metrics["last_error"]:
            return

        payload = {"events": metrics["events"], "interval_s": self.metrics_period}
        if metrics["avg_latency_ms"] is not None:
            payload["avg_latency_ms"] = metrics["avg_latency_ms"]
        if metrics["max_latency_ms"] is not None:
            payload["max_latency_ms"] = metrics["max_latency_ms"]

        meta: dict[str, Any] = {
            "stream_key": self.stream_key,
            "latency_samples": metrics["latency_samples"],
            "last_publish_ts_ns": metrics["last_publish_ts_ns"],
        }
        if metrics["last_error"]:
            meta["last_error"] = metrics["last_error"]
            self._record_metrics_error(metrics["last_error"])
        else:
            self._record_metrics_error(None)

        envelope = EventEnvelope(
            event_type="service_metrics",
            topic=f"service.{self.service_name}",
            symbol=self.service_name,
            ts_ns=time.time_ns(),
            payload=payload,
            meta=meta,
        )

        try:
            backend.write_event(envelope)
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.warning("Failed to write metrics", error=str(exc))
            self._record_metrics_error(str(exc))

    async def _resolve_metrics_backend(
        self, config: MarketDataServiceConfig
    ) -> PersistenceBackend | None:
        if self._metrics_backend_factory is not None:
            backend_obj = self._metrics_backend_factory(config)
            backend = await backend_obj if inspect.isawaitable(backend_obj) else backend_obj
            return backend

        url = os.getenv("MARKET_DATA_METRICS_INFLUX_URL")
        bucket = os.getenv("MARKET_DATA_METRICS_INFLUX_BUCKET")
        org = os.getenv("MARKET_DATA_METRICS_INFLUX_ORG")
        if not (url and bucket and org):
            return None

        token = os.getenv("MARKET_DATA_METRICS_INFLUX_TOKEN")
        measurement = os.getenv("MARKET_DATA_METRICS_MEASUREMENT", "service_metrics")
        timeout_raw = os.getenv("MARKET_DATA_METRICS_INFLUX_TIMEOUT_MS")
        timeout = None
        if timeout_raw:
            try:
                timeout_candidate = int(timeout_raw)
            except ValueError:
                logger.warning("Invalid metrics timeout provided", timeout=timeout_raw)
            else:
                if timeout_candidate > 0:
                    timeout = timeout_candidate

        try:
            return InfluxPersistenceBackend(
                url,
                bucket=bucket,
                org=org,
                token=token,
                measurement=measurement,
                timeout=timeout,
            )
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.warning("Failed to initialise metrics backend", error=str(exc))
            self._record_metrics_error(str(exc))
            return None

    def _record_stream_error(self, error: str | None) -> None:
        self.health_state["last_stream_error"] = error
        if error:
            self.health_state["last_error"] = error

    def _record_metrics_error(self, error: str | None) -> None:
        self.health_state["last_metrics_error"] = error
        if error:
            self.health_state["last_error"] = error

    def get_snapshot(self, symbol: str) -> dict[str, Any] | None:
        """Return a deepcopy of the snapshot for *symbol* if available."""

        snapshot = self._snapshots.get(symbol)
        if snapshot is None:
            return None
        return deepcopy(snapshot)

    def get_snapshots(self, *, stale: bool | None = None) -> list[dict[str, Any]]:
        """Return a list of snapshots, optionally filtered by stale flag."""

        items = []
        for symbol in sorted(self._snapshots):
            snapshot = self._snapshots[symbol]
            if stale is not None:
                if bool(snapshot.get("stale")) is not stale:
                    continue
            items.append(deepcopy(snapshot))
        return items

    async def get_metrics_summary(self) -> dict[str, Any] | None:
        """Return the current metrics summary without resetting counters."""

        publisher = self.stream_publisher
        if publisher is None:
            return None
        try:
            return await publisher.get_summary()
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.warning("Failed to collect metrics summary", error=str(exc))
            self._record_metrics_error(str(exc))
            return None

    async def get_health_snapshot(self) -> dict[str, Any]:
        """Return health information for diagnostics endpoints."""

        redis_status: dict[str, Any]
        redis = self.redis
        if redis is None:
            redis_status = {"connected": False, "error": "not_initialised"}
        else:
            try:
                ping_result = await redis.ping()
            except Exception as exc:  # pragma: no cover - defensive logging
                redis_status = {"connected": False, "error": str(exc)}
            else:
                redis_status = {"connected": bool(ping_result), "error": None}

        metrics = await self.get_metrics_summary()

        return {
            "subscriptions": len(self._snapshots),
            "redis": redis_status,
            "metrics": metrics,
            "health": deepcopy(self.health_state),
        }
