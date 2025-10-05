from datetime import datetime, timezone

import pytest

from src.db import connect, run_sql_query


@pytest.mark.asyncio
async def test_connect_uses_injected_engine(sqlite_db) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()

    with connect() as connection:
        connection.execute(
            "INSERT INTO orders (symbol, status, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("ES", "Submitted", timestamp, timestamp),
        )
        connection.commit()

    row = sqlite_db.execute(
        "SELECT symbol, status FROM orders WHERE symbol = ?",
        ("ES",),
    ).fetchone()
    assert tuple(row) == ("ES", "Submitted")


@pytest.mark.asyncio
async def test_run_sql_query_returns_dicts(sqlite_db) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    sqlite_db.execute(
        "INSERT INTO orders (symbol, status, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("NQ", "Filled", timestamp, timestamp),
    )
    sqlite_db.commit()

    rows = run_sql_query(
        "SELECT symbol, status FROM orders WHERE symbol = :symbol",
        {"symbol": "NQ"},
    )
    assert rows == [{"symbol": "NQ", "status": "Filled"}]
