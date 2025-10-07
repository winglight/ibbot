"""Persistence backends for the unified data layer."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from loguru import logger

from .event_bus import EventEnvelope
from src.ibbot.storage.parquet_writer import MarketDataParquetWriter, get_market_data_writer

__all__ = [
    "PersistenceBackend",
    "NoopPersistenceBackend",
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
            logger.exception("Failed to append candle to Parquet store: %s", exc)

    def load_candles(
        self,
        symbol: str,
        interval: str,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        return self._writer.read_candles(symbol, interval, limit=limit)
