import asyncio
import os
import sqlite3
from pathlib import Path

import pytest

from src.data_layer.event_bus import EventEnvelope
from src.data_layer.persistence import MariaDbPersistenceBackend
from src.db import market_event_writer, service_metric_writer
from src.db.mariadb import set_engine


@pytest.fixture(autouse=True)
def configure_sqlite(tmp_path: Path) -> None:
    db_path = tmp_path / "ibbot_integration.db"
    os.environ["IBBOT_DB_URL"] = f"sqlite:///{db_path}"
    os.environ["DATA_LAYER_DB_URL"] = os.environ["IBBOT_DB_URL"]
    set_engine(None)

    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
            """
            CREATE TABLE market_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT,
                topic TEXT,
                symbol TEXT,
                source TEXT,
                payload TEXT,
                meta TEXT,
                ts_ns INTEGER,
                recorded_at TEXT,
                ingested_at TEXT
            );
            CREATE TABLE service_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service TEXT,
                metric TEXT,
                value REAL,
                status TEXT,
                host TEXT,
                details TEXT,
                recorded_at TEXT
            );
            """
        )
    finally:
        connection.close()

    yield

    set_engine(None)


@pytest.mark.asyncio
async def test_mariadb_persistence_backend_records_events_and_metrics() -> None:
    backend = MariaDbPersistenceBackend(
        repository_factory=market_event_writer,
        metric_repository_factory=service_metric_writer,
    )

    envelope = EventEnvelope(
        event_type="market.candle",
        topic="candles.AAPL",
        symbol="AAPL",
        payload={
            "interval": "1m",
            "open": 150.0,
            "high": 151.0,
            "low": 149.5,
            "close": 150.5,
            "interval_end_ns": 1_700_000_000_000_000_000,
        },
        meta={"interval": "1m"},
        ts_ns=1_700_000_000_000_000_000,
    )

    backend.write_event(envelope)
    backend.record_service_metrics(
        {
            "service": "market-data",
            "metric": "events-written",
            "value": 1,
        }
    )

    await asyncio.sleep(0.05)

    with sqlite3.connect(os.environ["IBBOT_DB_URL"].replace("sqlite:///", "")) as connection:
        event_row = connection.execute(
            "SELECT event_type, topic FROM market_events"
        ).fetchone()
        metric_row = connection.execute(
            "SELECT service, metric, value FROM service_metrics"
        ).fetchone()

    assert event_row == ("market.candle", "candles.AAPL")
    assert metric_row == ("market-data", "events-written", 1.0)
