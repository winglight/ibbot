"""Persistence backends for the unified data layer."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Callable, Iterable, Mapping

from loguru import logger

from .event_bus import EventEnvelope
from src.ibbot.storage.parquet_writer import MarketDataParquetWriter, get_market_data_writer

try:  # pragma: no cover - optional dependency
    from sqlalchemy.ext.asyncio import AsyncSession
except ImportError:  # pragma: no cover - optional dependency
    AsyncSession = None  # type: ignore

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.ibbot.db.repositories.events import MarketEventRepository
    from src.ibbot.db.repositories.metrics import ServiceMetricRepository

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
        repository: MarketEventRepository | None = None,
        session_factory: Callable[[], "AsyncSession"] | None = None,
        metric_repository: "ServiceMetricRepository" | None = None,
        metric_session_factory: Callable[[], "AsyncSession"] | None = None,
    ) -> None:
        if repository is None and session_factory is None:
            raise ValueError("repository or session_factory must be provided")

        self._repository = repository
        self._session_factory = session_factory
        self._metric_repository = metric_repository
        self._metric_session_factory = metric_session_factory or session_factory

    async def _persist(self, envelope: EventEnvelope) -> None:
        if self._repository is not None:
            await self._repository.insert_event(envelope)
            return

        if self._session_factory is None:  # pragma: no cover - defensive guard
            raise RuntimeError("Session factory is not configured")

        session = self._session_factory()
        try:
            from src.ibbot.db.repositories.events import MarketEventRepository

            repository = MarketEventRepository(session)
            await repository.insert_event(envelope)
        finally:
            try:
                await session.close()
            except Exception:  # pragma: no cover - defensive cleanup
                logger.debug(
                    "Failed to close MariaDB session during cleanup",
                    exc_info=True,
                )

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
        repository = self._metric_repository
        if repository is not None:
            await repository.record_metrics(metrics)
            return

        if self._metric_session_factory is None:  # pragma: no cover - defensive guard
            raise RuntimeError("Metric session factory is not configured")

        session = self._metric_session_factory()
        try:
            from src.ibbot.db.repositories.metrics import ServiceMetricRepository

            repository = ServiceMetricRepository(session)
            await repository.record_metrics(metrics)
        finally:
            try:
                await session.close()
            except Exception:  # pragma: no cover - defensive cleanup
                logger.debug(
                    "Failed to close MariaDB session during metric persistence cleanup",
                    exc_info=True,
                )

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
