"""Runtime management for the market data service."""

from __future__ import annotations

import asyncio
import inspect
from contextlib import suppress
from typing import Any, Awaitable, Callable, Protocol

from loguru import logger

from src.cache import ensure_redis
from src.configuration.provider import (
    ConfigProvider,
    MarketDataServiceConfig,
    get_provider,
)

from .subscriptions import PubSubListener


class SupportsLifecycle(Protocol):
    """Simple protocol describing listener objects with start/stop hooks."""

    async def start(self) -> None:
        ...

    async def stop(self) -> None:
        ...


ListenerFactory = Callable[[MarketDataServiceConfig, "MarketDataRuntime"], Awaitable[SupportsLifecycle] | SupportsLifecycle]
MetricsFactory = Callable[["MarketDataRuntime"], Awaitable[Any]]
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
        service_name: str | None = None,
        stream_key: str | None = None,
        metrics_period: float | None = None,
    ) -> None:
        self._config_provider_factory = config_provider_factory or get_provider
        self._redis_factory = redis_factory
        self._listener_factory = listener_factory
        self._metrics_factory = metrics_factory
        self._lock = asyncio.Lock()
        self._shutdown_event = asyncio.Event()

        self.config_provider: ConfigProvider[MarketDataServiceConfig] | None = None
        self.config: MarketDataServiceConfig | None = None
        self.redis: Any = None
        self.listener: SupportsLifecycle | None = None
        self.metrics_task: asyncio.Task[Any] | None = None
        self._snapshots: dict[str, dict[str, Any]] = {}

        self.service_name = service_name
        self.stream_key = stream_key
        self.metrics_period = metrics_period
        self._started = False

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

            if self._metrics_factory is not None:
                metrics_coro = self._metrics_factory(self)
                if not inspect.isawaitable(metrics_coro):
                    raise TypeError("metrics_factory must return an awaitable")
                self.metrics_task = asyncio.create_task(metrics_coro)
            elif self.metrics_period is not None:
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

            self.redis = None
            self._started = False
            logger.info("Market data runtime stopped", service=self.service_name)

    async def _default_metrics_loop(self) -> None:
        """Placeholder metrics task that sleeps on the configured interval."""

        assert self.metrics_period is not None
        try:
            while not self._shutdown_event.is_set():
                await asyncio.sleep(self.metrics_period)
        except asyncio.CancelledError:
            raise
