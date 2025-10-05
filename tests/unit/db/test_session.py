from datetime import datetime, timezone

import pytest

from src.db import connect, run_sql_query


@pytest.mark.asyncio
async def test_connect_uses_injected_engine(sqlite_db) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()

    with connect() as connection:
        connection.execute(
            "INSERT INTO market_events (event_type, topic, recorded_at, ingested_at) VALUES (?, ?, ?, ?)",
            ("test.event", "topic", timestamp, timestamp),
        )
        connection.commit()

    row = sqlite_db.execute(
        "SELECT event_type, topic FROM market_events WHERE event_type = ?",
        ("test.event",),
    ).fetchone()
    assert tuple(row) == ("test.event", "topic")


@pytest.mark.asyncio
async def test_run_sql_query_returns_dicts(sqlite_db) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    sqlite_db.execute(
        "INSERT INTO market_events (event_type, topic, recorded_at, ingested_at) VALUES (?, ?, ?, ?)",
        ("queue-monitor", "queue-length", timestamp, timestamp),
    )
    sqlite_db.commit()

    rows = run_sql_query(
        "SELECT event_type, topic FROM market_events WHERE event_type = :event_type",
        {"event_type": "queue-monitor"},
    )
    assert rows == [{"event_type": "queue-monitor", "topic": "queue-length"}]
