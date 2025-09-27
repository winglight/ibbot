"""Facade for managing unified market data sources."""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional

from loguru import logger

from .event_bus import EventBus

__all__ = [
    "DataSubscriptionRequest",
    "DataSubscriptionToken",
    "DataSourceAdapter",
    "DataSourceError",
    "DataSourceManager",
]

CancelCallback = Callable[[], Optional[Awaitable[None]]]


@dataclass(frozen=True)
class DataSubscriptionToken:
    """Handle returned to consumers for managing subscriptions."""

    token: str
    channel: str


@dataclass(frozen=True)
class DataSubscriptionRequest:
    """Standardised parameters for requesting data subscriptions."""

    channel: str
    symbol: Optional[str] = None
    interval: Optional[str] = None
    options: Dict[str, Any] = field(default_factory=dict)

    _DEDUP_IGNORE_OPTIONS = frozenset({"force_restart"})

    def dedupe_key(self) -> tuple[Any, ...]:
        """Return a hashable key for deduplicating subscriptions."""

        if self.options:
            items = [
                (key, value)
                for key, value in self.options.items()
                if key not in self._DEDUP_IGNORE_OPTIONS
            ]
            options_key = tuple(sorted(items))
        else:
            options_key = ()
        return (
            self.channel,
            self.symbol,
            self.interval,
            options_key,
        )


class DataSourceError(RuntimeError):
    """Base class for data source related errors."""


class DataSourceAdapter:
    """Interface expected from concrete data adapters."""

    channel: str

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus

    @property
    def event_bus(self) -> EventBus:
        return self._event_bus

    async def subscribe(self, request: DataSubscriptionRequest) -> CancelCallback:
        """Start streaming data for *request*.

        Returns a callback used to cancel the underlying subscription.  The
        default implementation signals that concrete adapters must override
        the method.
        """

        raise NotImplementedError

    async def snapshot(self, request: DataSubscriptionRequest) -> Any:
        """Return the latest cached snapshot for *request*."""

        raise NotImplementedError

    async def history(self, request: DataSubscriptionRequest) -> Any:
        """Return historical data for *request*."""

        raise NotImplementedError


@dataclass
class _RegistryEntry:
    cancel_callback: CancelCallback
    refcount: int = 1


class _SubscriptionRegistry:
    """Track active subscriptions and manage reference counts."""

    def __init__(self) -> None:
        self._entries: Dict[tuple[Any, ...], _RegistryEntry] = {}
        self._token_to_key: Dict[str, tuple[Any, ...]] = {}
        self._token_counter = itertools.count(1)
        self._lock = asyncio.Lock()

    async def acquire(
        self,
        request: DataSubscriptionRequest,
        factory: Callable[[], Awaitable[CancelCallback]],
    ) -> tuple[DataSubscriptionToken, bool]:
        """Return a subscription token, creating the subscription if needed.

        Returns a tuple ``(token, is_new)`` where ``is_new`` indicates whether
        ``factory`` was invoked to create a fresh underlying subscription.
        """

        key = request.dedupe_key()
        async with self._lock:
            entry = self._entries.get(key)
            if entry:
                token_value = f"sub-{next(self._token_counter)}"
                self._token_to_key[token_value] = key
                entry.refcount += 1
                logger.debug(
                    "SubscriptionRegistry reuse key=%s refcount=%s", key, entry.refcount
                )
                return DataSubscriptionToken(token=token_value, channel=request.channel), False

        # Create outside the lock to avoid holding it while awaiting.
        cancel_callback = await factory()

        async with self._lock:
            entry = self._entries.get(key)
            if entry:
                # Another subscriber raced to create the same key.  Reuse it and
                # invoke the redundant cancel callback immediately.
                try:
                    maybe_await = cancel_callback()
                    if asyncio.iscoroutine(maybe_await):
                        await maybe_await
                except Exception as exc:  # pragma: no cover - defensive guard
                    logger.exception("Failed to cancel redundant subscription: {}", exc)
                entry.refcount += 1
                token_value = f"sub-{next(self._token_counter)}"
                self._token_to_key[token_value] = key
                logger.debug(
                    "SubscriptionRegistry reuse-after-race key=%s refcount=%s", key, entry.refcount
                )
                return DataSubscriptionToken(token=token_value, channel=request.channel), False

            token_value = f"sub-{next(self._token_counter)}"
            self._entries[key] = _RegistryEntry(cancel_callback=cancel_callback, refcount=1)
            self._token_to_key[token_value] = key
            logger.debug("SubscriptionRegistry new key=%s", key)
            return DataSubscriptionToken(token=token_value, channel=request.channel), True

    async def release(self, token: DataSubscriptionToken) -> None:
        key = self._token_to_key.pop(token.token, None)
        if key is None:
            return

        async with self._lock:
            entry = self._entries.get(key)
            if not entry:
                return

            entry.refcount -= 1
            logger.debug(
                "SubscriptionRegistry release key=%s refcount=%s", key, entry.refcount
            )
            if entry.refcount > 0:
                return

            self._entries.pop(key, None)

        try:
            maybe_await = entry.cancel_callback()
            if asyncio.iscoroutine(maybe_await):
                await maybe_await
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.exception("Error cancelling subscription: {}", exc)


class DataSourceManager:
    """Coordinate subscriptions across concrete adapters."""

    def __init__(self, event_bus: Optional[EventBus] = None) -> None:
        self._event_bus = event_bus or EventBus()
        self._registry = _SubscriptionRegistry()
        self._adapters: Dict[str, DataSourceAdapter] = {}

    @property
    def event_bus(self) -> EventBus:
        return self._event_bus

    def register_source(self, adapter: DataSourceAdapter) -> None:
        channel = getattr(adapter, "channel", None)
        if not channel:
            raise ValueError("adapter.channel must be defined")
        if channel in self._adapters:
            raise ValueError(f"Adapter already registered for channel '{channel}'")
        self._adapters[channel] = adapter
        logger.info("Registered data source adapter for channel=%s", channel)

    async def subscribe(self, request: DataSubscriptionRequest) -> DataSubscriptionToken:
        adapter = self._adapters.get(request.channel)
        if not adapter:
            raise DataSourceError(f"No adapter registered for channel '{request.channel}'")

        token, is_new = await self._registry.acquire(request, lambda: adapter.subscribe(request))
        if is_new:
            logger.info("Started new subscription for channel=%s symbol=%s", request.channel, request.symbol)
        else:
            logger.info(
                "Reused subscription for channel=%s symbol=%s", request.channel, request.symbol
            )
        return token

    async def unsubscribe(self, token: DataSubscriptionToken) -> None:
        await self._registry.release(token)

    async def snapshot(self, request: DataSubscriptionRequest) -> Any:
        adapter = self._adapters.get(request.channel)
        if not adapter:
            raise DataSourceError(f"No adapter registered for channel '{request.channel}'")
        return await adapter.snapshot(request)

    async def history(self, request: DataSubscriptionRequest) -> Any:
        adapter = self._adapters.get(request.channel)
        if not adapter:
            raise DataSourceError(f"No adapter registered for channel '{request.channel}'")
        return await adapter.history(request)

