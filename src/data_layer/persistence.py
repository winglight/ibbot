"""Persistence backends for the unified data layer."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable, Mapping
from typing import Any

from loguru import logger

from .event_bus import EventEnvelope
from src.ibbot.db import get_session_factory, market_event_repository, service_metric_repository
from src.ibbot.storage.parquet_writer import MarketDataParquetWriter, get_market_data_writer

__all__ = [
    "PersistenceBackend",
    "NoopPersistenceBackend",
    "DatabasePersistenceBackend",
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


class DatabasePersistenceBackend(PersistenceBackend):
    """MariaDB-backed persistence layer for market events and service metrics."""

    def __init__(self, *, session_factory=None) -> None:
        self._session_factory = session_factory or get_session_factory()

    async def _persist_market_event(self, envelope: EventEnvelope) -> None:
        session = self._session_factory()
        try:
            repository = market_event_repository(session)
            await repository.insert_event(
                {
                    "event_type": envelope.event_type,
                    "topic": envelope.topic,
                    "symbol": envelope.symbol,
                    "source": envelope.source,
                    "payload": envelope.payload,
                    "meta": envelope.meta,
                    "ts_ns": envelope.ts_ns or time.time_ns(),
                }
            )
        finally:
            await session.close()

    async def _persist_service_metrics(self, payload: Mapping[str, Any] | Iterable[Mapping[str, Any]]) -> None:
        session = self._session_factory()
        try:
            repository = service_metric_repository(session)
            await repository.record_metrics(payload)
        finally:
            await session.close()

    async def _persist(self, envelope: EventEnvelope) -> None:
        if envelope.event_type.startswith("service.metric"):
            metrics_payload: Any = envelope.payload
            if isinstance(metrics_payload, Mapping):
                await self._persist_service_metrics(metrics_payload)
            elif isinstance(metrics_payload, Iterable) and not isinstance(
                metrics_payload, (str, bytes)
            ):
                await self._persist_service_metrics(metrics_payload)
            else:
                logger.warning(
                    "Ignoring service metric event with unsupported payload", event_type=envelope.event_type
                )
            return

        await self._persist_market_event(envelope)

    async def _persist_with_logging(self, envelope: EventEnvelope) -> None:
        try:
            await self._persist(envelope)
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.warning(
                "Failed to persist data layer event: {}", exc, event_type=envelope.event_type, topic=envelope.topic
            )

    def write_event(self, envelope: EventEnvelope) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self._persist_with_logging(envelope))
        else:
            loop.create_task(self._persist_with_logging(envelope))


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
