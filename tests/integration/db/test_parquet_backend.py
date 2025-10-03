from __future__ import annotations

from pathlib import Path

from src.data_layer.event_bus import EventEnvelope
from src.data_layer.persistence import ParquetPersistenceBackend
from src.ibbot.storage.parquet_writer import MarketDataParquetWriter


def test_parquet_backend_writes_and_reads(tmp_path: Path) -> None:
    writer = MarketDataParquetWriter(base_dir=tmp_path)
    backend = ParquetPersistenceBackend(writer)

    envelope = EventEnvelope(
        event_type="market.candle",
        topic="candles.AAPL",
        symbol="AAPL",
        payload={
            "interval": "1m",
            "open": 100.0,
            "high": 101.0,
            "low": 99.5,
            "close": 100.5,
            "volume": 500,
            "interval_start_ns": 1_700_000_000_000_000_000,
            "interval_end_ns": 1_700_000_000_060_000_000,
        },
        meta={"interval": "1m"},
        ts_ns=1_700_000_000_060_000_000,
    )

    backend.write_event(envelope)

    candles = backend.load_candles("AAPL", "1m")
    assert len(candles) == 1
    assert candles[0]["close"] == 100.5
    assert candles[0]["interval"] == "1m"
