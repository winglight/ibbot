import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.db import market_event_writer, order_repository, service_metric_writer
from src.db.mariadb import set_engine


@pytest.fixture(autouse=True)
def _isolate_connection(tmp_path: Path) -> None:
    db_path = tmp_path / "ibbot_test.db"
    os.environ["IBBOT_DB_URL"] = f"sqlite:///{db_path}"
    os.environ["DATA_LAYER_DB_URL"] = os.environ["IBBOT_DB_URL"]
    set_engine(None)

    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
            """
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT UNIQUE,
                symbol TEXT,
                side TEXT,
                status TEXT,
                exchange TEXT,
                quantity REAL,
                price REAL,
                order_timestamp TEXT,
                raw_payload TEXT,
                created_at TEXT,
                updated_at TEXT,
                filled_quantity REAL,
                remaining_quantity REAL,
                avg_fill_price REAL,
                limit_price REAL,
                stop_price REAL,
                commission REAL,
                realized_pnl REAL,
                unrealized_pnl REAL,
                trigger_price REAL,
                order_type TEXT,
                trigger TEXT,
                trigger_type TEXT,
                trigger_status TEXT,
                notes TEXT,
                account TEXT,
                source TEXT,
                strategy TEXT,
                sec_type TEXT
            );
            CREATE UNIQUE INDEX ix_orders_order_id ON orders(order_id);

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

            CREATE TABLE market_prices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT,
                bid REAL,
                ask REAL,
                bid_size REAL,
                ask_size REAL,
                source TEXT,
                observed_at TEXT,
                extra TEXT
            );
            """
        )
    finally:
        connection.close()

    yield

    set_engine(None)


@pytest.mark.asyncio
async def test_order_repository_upserts_payload() -> None:
    repo = order_repository()
    payload = {
        "order_id": "1001",
        "symbol": "AAPL",
        "side": "BUY",
        "quantity": 10,
        "price": 150.0,
        "status": "submitted",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await repo.upsert_order(payload)

    updated_payload = dict(payload, status="filled", filled_quantity=10, price=151.25)
    await repo.upsert_order(updated_payload)

    with sqlite3.connect(os.environ["IBBOT_DB_URL"].replace("sqlite:///", "")) as connection:
        row = connection.execute(
            "SELECT status, filled_quantity FROM orders WHERE order_id = ?", ("1001",)
        ).fetchone()
    assert row == ("filled", pytest.approx(10.0))


@pytest.mark.asyncio
async def test_market_event_repository_inserts_event() -> None:
    repo = market_event_writer()
    event = {
        "event_type": "market.quote",
        "topic": "quotes.AAPL",
        "symbol": "AAPL",
        "source": "unit-test",
        "payload": {"bid": 100.5, "ask": 100.6},
        "meta": {"interval": "1s"},
        "ts_ns": 1_700_000_000_000_000_000,
    }

    await repo.insert_event(event)

    with sqlite3.connect(os.environ["IBBOT_DB_URL"].replace("sqlite:///", "")) as connection:
        row = connection.execute(
            "SELECT event_type, topic, symbol, ts_ns FROM market_events"
        ).fetchone()
    assert row == ("market.quote", "quotes.AAPL", "AAPL", 1_700_000_000_000_000_000)


@pytest.mark.asyncio
async def test_service_metric_repository_records_metrics() -> None:
    repo = service_metric_writer()
    metrics = [
        {
            "service": "market-data",
            "metric": "latency",
            "value": 0.25,
            "details": {"updates": 10},
        },
        {
            "service": "market-data",
            "metric": "throughput",
            "value": 1200,
        },
    ]

    await repo.record_metrics(metrics)

    with sqlite3.connect(os.environ["IBBOT_DB_URL"].replace("sqlite:///", "")) as connection:
        rows = connection.execute("SELECT metric, value FROM service_metrics").fetchall()
    assert {row[0] for row in rows} == {"latency", "throughput"}
