"""Factories and adapters for service persistence dependencies."""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from functools import partial
from typing import Any, Awaitable, Callable, Iterable, Mapping, Protocol, Sequence

from src.ibbot.redis import DEFAULT_SERVICE_METRICS_TTL, record_service_metrics

__all__ = [
    "ServiceMetricsWriterProtocol",
    "ServiceMetricsWriterAdapter",
    "create_service_metrics_writer",
    "MarketPriceRepositoryProtocol",
    "MarketPriceRepositoryAdapter",
    "create_market_price_repository",
]


class ServiceMetricsWriterProtocol(Protocol):
    """Minimal protocol for objects that can persist service metrics."""

    async def record_metrics(
        self,
        metrics: Mapping[str, Any] | Iterable[Mapping[str, Any]],
    ) -> None:
        """Persist one or more metrics entries."""


class MarketPriceRepositoryProtocol(Protocol):
    """Protocol for repositories capable of storing market prices."""

    async def insert_price(self, /, **payload: Any) -> None:
        """Persist a market price snapshot."""


@dataclass(slots=True)
class _RepositoryAdapter:
    """Base adapter that provides ``close``/``aclose`` helpers."""

    _instance: Any

    def __post_init__(self) -> None:
        self._close_callable = _resolve_close_callable(self._instance)

    @property
    def wrapped(self) -> Any:
        """Return the wrapped repository instance."""

        return self._instance

    async def aclose(self) -> None:
        """Asynchronously close the wrapped repository if possible."""

        closer = self._close_callable
        if closer is None:
            return
        result = closer()
        if inspect.isawaitable(result):
            await result

    def close(self) -> Awaitable[None]:
        """Return an awaitable that closes the repository."""

        return self.aclose()


class ServiceMetricsWriterAdapter(_RepositoryAdapter):
    """Adapter that normalises metric writers to an async interface."""

    def __post_init__(self) -> None:
        super().__post_init__()
        self._record_metrics = _resolve_callable(
            self._instance, ("record_metrics", "write_metrics", "record")
        )

    async def record_metrics(
        self,
        metrics: Mapping[str, Any] | Iterable[Mapping[str, Any]],
    ) -> None:
        await _invoke_callable(self._record_metrics, metrics)


class RedisServiceMetricWriter:
    """Lightweight sink that stores metrics in Redis with a 1-day TTL."""

    __slots__ = ("_logger", "_ttl")

    def __init__(
        self,
        *,
        logger_name: str | None = None,
        ttl_seconds: int = DEFAULT_SERVICE_METRICS_TTL,
    ) -> None:
        self._logger = logging.getLogger(logger_name or "services.metrics")
        self._ttl = ttl_seconds

    async def record_metrics(
        self,
        metrics: Mapping[str, Any] | Iterable[Mapping[str, Any]],
    ) -> None:
        try:
            await record_service_metrics(metrics, ttl_seconds=self._ttl)
        except Exception:
            self._logger.warning("Failed to record service metrics", exc_info=True)
            raise


class MarketPriceRepositoryAdapter(_RepositoryAdapter):
    """Adapter exposing an async interface for price persistence."""

    def __post_init__(self) -> None:
        super().__post_init__()
        self._insert_price = _resolve_callable(
            self._instance, ("insert_price", "write_price", "save_price")
        )

    async def insert_price(self, /, **payload: Any) -> None:
        await _invoke_callable(self._insert_price, offload=True, **payload)


async def _invoke_callable(
    func: Callable[..., Any],
    /,
    *args: Any,
    offload: bool = True,
    **kwargs: Any,
) -> Any:
    """Invoke *func* ensuring synchronous callables run off the loop."""

    if inspect.iscoroutinefunction(func):
        result = func(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    if offload:
        call = partial(func, *args, **kwargs)
        result = await asyncio.to_thread(call)
    else:
        result = func(*args, **kwargs)

    if inspect.isawaitable(result):
        return await result
    return result


def _resolve_callable(obj: Any, names: Sequence[str]) -> Callable[..., Any]:
    for name in names:
        candidate = getattr(obj, name, None)
        if callable(candidate):
            return candidate
    raise AttributeError(
        f"Object {obj!r} does not implement any of: {', '.join(names)}"
    )


def _resolve_close_callable(
    obj: Any,
) -> Callable[[], Awaitable[Any] | Any] | None:
    for name in ("aclose", "close"):
        candidate = getattr(obj, name, None)
        if callable(candidate):
            return candidate
    session = getattr(obj, "_session", None)
    if session is not None:
        for name in ("aclose", "close"):
            candidate = getattr(session, name, None)
            if callable(candidate):
                return candidate
    return None


async def create_service_metrics_writer(
    factory: Callable[[], Awaitable[Any] | Any] | None = None,
    *,
    logger_name: str | None = None,
) -> ServiceMetricsWriterAdapter | None:
    """Instantiate a service metrics writer adapter.

    The function resolves the repository factory lazily, defaulting to the
    :func:`src.db.service_metric_writer` helper when available. Any synchronous
    repository implementation is wrapped so writes execute outside the running
    event loop to avoid blocking service tasks.
    """

    repository_factory = factory
    if repository_factory is None:
        try:
            from src.db import service_metric_writer as default_factory
        except ModuleNotFoundError:
            return None

        if logger_name is not None:
            repository_factory = lambda: default_factory(logger_name=logger_name)
        else:
            repository_factory = default_factory

    repository = repository_factory()
    if inspect.isawaitable(repository):
        repository = await repository

    if repository is None:
        return None
    if isinstance(repository, ServiceMetricsWriterAdapter):
        return repository
    return ServiceMetricsWriterAdapter(repository)


async def create_market_price_repository(
    factory: Callable[[], Awaitable[Any] | Any] | None = None,
    *,
    logger_name: str | None = None,
) -> MarketPriceRepositoryAdapter | None:
    """Return an adapter providing async access to a market price repository."""

    repository_factory = factory
    if repository_factory is None:
        try:
            from src.db import market_price_writer as default_factory
        except ModuleNotFoundError:
            return None

        if logger_name is not None:
            repository_factory = lambda: default_factory(logger_name=logger_name)
        else:
            repository_factory = default_factory

    repository = repository_factory()
    if inspect.isawaitable(repository):
        repository = await repository

    if repository is None:
        return None
    if isinstance(repository, MarketPriceRepositoryAdapter):
        return repository
    return MarketPriceRepositoryAdapter(repository)
