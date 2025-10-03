"""Persistence backends for the unified data layer."""

from __future__ import annotations

import asyncio
import inspect
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Iterable, Mapping

from loguru import logger

from .event_bus import EventEnvelope
from src.ibbot.storage.parquet_writer import MarketDataParquetWriter, get_market_data_writer

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.db import MarketEventRepository, ServiceMetricRepository

__all__ = [
    "PersistenceBackend",
    "NoopPersistenceBackend",
    "MariaDbPersistenceBackend",
    "ParquetPersistenceBackend",
]


class PersistenceBackend:
    """Abstract interface for persisting market data events."""

    def write_event(self, envelope: EventEnvelope) -> None:
        raise NotImplementedError


class NoopPersistenceBackend(PersistenceBackend):
    """Persistence backend that simply drops events (default for tests)."""

    def write_event(self, envelope: EventEnvelope) -> None:  # pragma: no cover - trivial
        return None


class MariaDbPersistenceBackend(PersistenceBackend):
    """Persist market events into the MariaDB ``market_events`` table."""

    def __init__(
        self,
        *,
        repository: "MarketEventRepository" | None = None,
        repository_factory: (
            Callable[[], Awaitable["MarketEventRepository"] | "MarketEventRepository"]
            | None
        ) = None,
        metric_repository: "ServiceMetricRepository" | None = None,
        metric_repository_factory: (
            Callable[[], Awaitable["ServiceMetricRepository"] | "ServiceMetricRepository"]
            | None
        ) = None,
    ) -> None:
        if repository is None and repository_factory is None:
            raise ValueError("repository or repository_factory must be provided")

        self._repository = repository
        self._repository_factory = repository_factory
        self._metric_repository = metric_repository
        self._metric_repository_factory = metric_repository_factory

    async def _persist(self, envelope: EventEnvelope) -> None:
        repository = await self._get_event_repository()
        await repository.insert_event(envelope)

    @staticmethod
    def _log_task_error(task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except Exception as exc:  # pragma: no cover - logged for observability
            logger.exception("Failed to persist market event to MariaDB: {}", exc)

    def write_event(self, envelope: EventEnvelope) -> None:
        coro = self._persist(envelope)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(coro)
            return

        task = loop.create_task(coro)
        task.add_done_callback(self._log_task_error)

    async def _persist_metrics(
        self, metrics: Mapping[str, object] | Iterable[Mapping[str, object]]
    ) -> None:
        repository = await self._get_metric_repository()
        await repository.record_metrics(metrics)

    def record_service_metrics(
        self, metrics: Mapping[str, object] | Iterable[Mapping[str, object]]
    ) -> None:
        coro = self._persist_metrics(metrics)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(coro)
            return

        task = loop.create_task(coro)
        task.add_done_callback(self._log_task_error)

    async def _resolve_repository(
        self,
        repository: "MarketEventRepository | ServiceMetricRepository | None",
        factory: Callable[[], Awaitable[Any] | Any] | None,
    ) -> Any:
        if repository is not None:
            return repository
        if factory is None:
            raise RuntimeError("Repository factory is not configured")
        instance = factory()
        if inspect.isawaitable(instance):
            instance = await instance
        return instance

    async def _get_event_repository(self) -> "MarketEventRepository":
        return await self._resolve_repository(self._repository, self._repository_factory)

    async def _get_metric_repository(self) -> "ServiceMetricRepository":
        return await self._resolve_repository(
            self._metric_repository, self._metric_repository_factory
        )


class ParquetPersistenceBackend(PersistenceBackend):
    """Persist candle events to the on-disk Parquet store."""

    def __init__(self, writer: MarketDataParquetWriter | None = None) -> None:
        self._writer = writer or get_market_data_writer()

    def write_event(self, envelope: EventEnvelope) -> None:  # pragma: no cover - thin wrapper
        if envelope.event_type != "market.candle":
            return
        payload = envelope.payload or {}
        symbol = envelope.symbol or payload.get("symbol")
        interval = payload.get("interval")
        if interval is None and envelope.meta:
            interval = envelope.meta.get("interval")
        if not symbol or not interval:
            logger.debug(
                "Skipping candle persistence without symbol/interval: event=%s symbol=%s interval=%s",
                envelope.event_type,
                symbol,
                interval,
            )
            return
        timestamp_ns = envelope.ts_ns or payload.get("interval_end_ns")
        try:
            self._writer.append_candle(symbol, interval, payload, timestamp_ns=timestamp_ns)
        except Exception as exc:
            logger.exception("Failed to append candle to Parquet store: {}", exc)

    def load_candles(
        self,
        symbol: str,
        interval: str,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        return self._writer.read_candles(symbol, interval, limit=limit)
