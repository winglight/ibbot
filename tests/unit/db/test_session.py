from datetime import datetime, timezone

import pytest

from src.db import connect, run_sql_query


@pytest.mark.asyncio
async def test_connect_uses_injected_engine(sqlite_db) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()

    with connect() as connection:
        connection.execute(
            "INSERT INTO service_metrics (service, metric, value, recorded_at) VALUES (?, ?, ?, ?)",
            ("analytics", "jobs", 2.0, timestamp),
        )
        connection.commit()

    row = sqlite_db.execute(
        "SELECT service, metric, value FROM service_metrics WHERE service = ?",
        ("analytics",),
    ).fetchone()
    assert tuple(row) == ("analytics", "jobs", 2.0)


@pytest.mark.asyncio
async def test_run_sql_query_returns_dicts(sqlite_db) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    sqlite_db.execute(
        "INSERT INTO service_metrics (service, metric, value, recorded_at) VALUES (?, ?, ?, ?)",
        ("queue-monitor", "queue-length", 3.0, timestamp),
    )
    sqlite_db.commit()

    rows = run_sql_query(
        "SELECT service, metric, value FROM service_metrics WHERE service = :service",
        {"service": "queue-monitor"},
    )
    assert rows == [{"service": "queue-monitor", "metric": "queue-length", "value": 3.0}]
