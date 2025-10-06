from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from typing import Any

import pytest

from src.db.repositories.order import OrderRepository, _normalise_order_payload
from src.db.repositories.risk import RiskRulesRepository, _coerce_timestamp
from src.db.repositories.base import format_datetime_for_db


@pytest.mark.asyncio
async def test_order_repository_upserts_by_command_id(
    sqlite_db, order_repository_factory
) -> None:
    repo = await order_repository_factory()
    payload = {
        "command_id": "cmd-123",
        "symbol": "AAPL",
        "side": "BUY",
        "quantity": 10,
        "price": 150.0,
        "status": "submitted",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    inserted_id = await repo.upsert_order(payload)
    assert isinstance(inserted_id, int)

    row = sqlite_db.execute(
        "SELECT id, command_id, status FROM orders WHERE command_id = ?",
        ("cmd-123",),
    ).fetchone()
    assert row["id"] == inserted_id
    assert row["status"].lower() == "submitted"

    updated_payload = dict(
        payload,
        status="filled",
        filled_quantity=10,
        price=151.25,
    )
    updated_id = await repo.upsert_order(updated_payload)
    assert updated_id is None

    updated_row = sqlite_db.execute(
        "SELECT id, status, filled_quantity FROM orders WHERE command_id = ?",
        ("cmd-123",),
    ).fetchone()
    assert updated_row["id"] == inserted_id
    assert updated_row["status"].lower() == "filled"
    assert updated_row["filled_quantity"] == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_order_repository_respects_allow_insert_flag(
    sqlite_db, order_repository_factory
) -> None:
    repo = await order_repository_factory()

    payload = {
        "ib_order_id": "IB-SKIP-1",
        "symbol": "ES",
        "status": "Submitted",
    }

    skipped_id = await repo.upsert_order(payload, allow_insert=False)
    assert skipped_id is None

    row = sqlite_db.execute(
        "SELECT id FROM orders WHERE ib_order_id = ?",
        ("IB-SKIP-1",),
    ).fetchone()
    assert row is None

    inserted_id = await repo.upsert_order(payload, allow_insert=True)
    assert isinstance(inserted_id, int)

    row_after = sqlite_db.execute(
        "SELECT id, ib_order_id FROM orders WHERE id = ?",
        (inserted_id,),
    ).fetchone()
    assert row_after["ib_order_id"] == "IB-SKIP-1"


@pytest.mark.asyncio
async def test_order_repository_updates_existing_row_with_ib_identifier(
    sqlite_db, order_repository_factory
) -> None:
    repo = await order_repository_factory()

    initial_payload = {
        "command_id": "cmd-ib",
        "symbol": "MSFT",
        "side": "SELL",
        "quantity": 5,
        "status": "submitted",
    }
    inserted_id = await repo.upsert_order(initial_payload)
    assert isinstance(inserted_id, int)

    association_payload = {
        "command_id": "cmd-ib",
        "ib_order_id": "IB1001",
        "status": "accepted",
    }
    association_result = await repo.upsert_order(association_payload)
    assert association_result is None

    final_payload = {
        "ib_order_id": "IB1001",
        "status": "filled",
        "filled_quantity": 5,
        "avg_fill_price": 215.5,
    }
    final_result = await repo.upsert_order(final_payload)
    assert final_result is None

    row = sqlite_db.execute(
        "SELECT id, command_id, ib_order_id, status, filled_quantity "
        "FROM orders WHERE id = ?",
        (inserted_id,),
    ).fetchone()
    assert row["id"] == inserted_id
    assert row["command_id"] == "cmd-ib"
    assert row["ib_order_id"] == "IB1001"
    assert row["status"].lower() == "filled"
    assert row["filled_quantity"] == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_order_repository_updates_existing_row_with_client_identifier(
    sqlite_db, order_repository_factory
) -> None:
    repo = await order_repository_factory()

    initial_payload = {
        "command_id": "cmd-client-match",
        "client_order_id": 123456,
        "symbol": "NVDA",
        "status": "submitted",
    }

    inserted_id = await repo.upsert_order(initial_payload)
    assert isinstance(inserted_id, int)

    ib_payload = {
        "ib_order_id": "IB-CLIENT-1",
        "client_order_id": 123456,
        "status": "filled",
        "filled_quantity": 1,
    }

    update_result = await repo.upsert_order(ib_payload)
    assert update_result is None

    row = sqlite_db.execute(
        "SELECT id, client_order_id, ib_order_id, status, filled_quantity FROM orders "
        "WHERE id = ?",
        (inserted_id,),
    ).fetchone()

    assert row["id"] == inserted_id
    assert row["client_order_id"] == "123456"
    assert row["ib_order_id"] == "IB-CLIENT-1"
    assert row["status"].lower() == "filled"
    assert row["filled_quantity"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_order_repository_updates_existing_row_with_order_identifier(
    sqlite_db, order_repository_factory
) -> None:
    repo = await order_repository_factory()

    cursor = sqlite_db.execute(
        "INSERT INTO orders (order_id, symbol, status) VALUES (?, ?, ?)",
        ("6001", "NFLX", "submitted"),
    )
    sqlite_db.commit()
    existing_id = int(cursor.lastrowid)

    ib_payload = {
        "ib_order_id": "IB-ORDER-1",
        "order_id": 6001,
        "status": "cancelled",
    }

    update_result = await repo.upsert_order(ib_payload)
    assert update_result is None

    row = sqlite_db.execute(
        "SELECT id, order_id, client_order_id, ib_order_id, status FROM orders WHERE id = ?",
        (existing_id,),
    ).fetchone()

    assert row["id"] == existing_id
    assert row["order_id"] == "6001"
    assert row["client_order_id"] == "6001"
    assert row["ib_order_id"] == "IB-ORDER-1"
    assert row["status"].lower() == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exchange_value",
    [None, "", "   "],
    ids=["missing", "empty", "whitespace"],
)
async def test_order_repository_uses_default_exchange_when_value_missing(
    sqlite_db, order_repository_factory, exchange_value
) -> None:
    repo = await order_repository_factory()
    payload = {
        "command_id": f"cmd-exchange-{exchange_value!r}",
        "symbol": "TSLA",
        "side": "BUY",
        "quantity": 1,
        "status": "submitted",
    }
    if exchange_value is not None:
        payload["exchange"] = exchange_value

    normalised = _normalise_order_payload(payload)
    assert "exchange" not in normalised

    inserted_id = await repo.upsert_order(payload)
    assert isinstance(inserted_id, int)

    row = sqlite_db.execute(
        "SELECT exchange FROM orders WHERE id = ?",
        (inserted_id,),
    ).fetchone()

    assert row["exchange"] == "SMART"


@pytest.mark.asyncio
async def test_order_repository_persists_exchange_when_present(
    sqlite_db, order_repository_factory
) -> None:
    repo = await order_repository_factory()
    payload = {
        "command_id": "cmd-exchange-present",
        "symbol": "AMZN",
        "exchange": "NYSE",
    }

    inserted_id = await repo.upsert_order(payload)
    assert isinstance(inserted_id, int)

    row = sqlite_db.execute(
        "SELECT exchange FROM orders WHERE id = ?",
        (inserted_id,),
    ).fetchone()

    assert row["exchange"] == "NYSE"


class _LookupCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self._current: dict[str, Any] | None = None
        self.queries: list[tuple[str, str]] = []

    def execute(self, sql: str, params: tuple[str, ...]) -> None:
        where_clause = sql.split("WHERE", 1)[1]
        column = where_clause.split("=", 1)[0].strip().strip("`")
        value = params[0]
        self.queries.append((column, value))
        self._current = None
        for row in self._rows:
            if row.get(column) == value:
                self._current = {"id": row["id"]}
                break

    def fetchone(self) -> dict[str, Any] | None:
        return self._current


def test_find_existing_helpers_match_swapped_identifiers() -> None:
    repo = OrderRepository()
    rows = [
        {
            "id": 42,
            "command_id": None,
            "ib_order_id": None,
            "client_order_id": "CLIENT-123",
            "order_id": "ORDER-456",
        }
    ]
    cursor = _LookupCursor(rows)

    column, value = repo._find_existing_sqlite(
        cursor, None, None, "ORDER-456", "CLIENT-123"
    )

    assert column == "client_order_id"
    assert value == "CLIENT-123"
    assert ("client_order_id", "CLIENT-123") in cursor.queries

    cursor_mysql = _LookupCursor(rows)
    column_mysql, value_mysql = repo._find_existing_mariadb(
        cursor_mysql, None, None, "ORDER-456", "CLIENT-123"
    )

    assert column_mysql == "client_order_id"
    assert value_mysql == "CLIENT-123"
    assert ("client_order_id", "CLIENT-123") in cursor_mysql.queries


def test_coerce_timestamp_normalises_timezone() -> None:
    aware = datetime(2024, 1, 15, 12, 30, 45, tzinfo=timezone(timedelta(hours=2)))
    result = _coerce_timestamp(aware)

    assert isinstance(result, datetime)
    assert result.tzinfo is None
    assert result == aware.astimezone(timezone.utc).replace(tzinfo=None)


def test_coerce_timestamp_accepts_iso_string() -> None:
    result = _coerce_timestamp("2024-02-01T08:15:30+05:30")

    assert isinstance(result, datetime)
    assert result.tzinfo is None
    expected = datetime(2024, 2, 1, 2, 45, 30)
    assert result == expected


@pytest.mark.asyncio
async def test_risk_rules_repository_uses_naive_timestamp_for_mariadb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = RiskRulesRepository()

    async def immediate(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr("src.db.repositories.risk.run_in_thread", immediate)

    class FakeCursor:
        def execute(self, _sql, _params=None) -> None:
            return None

        def fetchall(self) -> list[tuple[str, ...]]:
            return []

        def close(self) -> None:
            return None

    class FakeConnection:
        def cursor(self) -> FakeCursor:
            return FakeCursor()

        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

    fake_connection = FakeConnection()

    from contextlib import contextmanager

    @contextmanager
    def fake_use_connection():
        yield fake_connection

    monkeypatch.setattr("src.db.repositories.risk.use_connection", fake_use_connection)
    monkeypatch.setattr("src.db.repositories.risk.is_sqlite", lambda _conn: False)

    captured_rows: list[dict[str, Any]] = []

    def fake_executemany(_connection, _sql, params_seq):
        captured_rows.extend(params_seq)

    monkeypatch.setattr("src.db.repositories.risk.executemany_write", fake_executemany)

    timestamp = datetime(2024, 3, 20, 14, 0, tzinfo=timezone(timedelta(hours=-4)))

    await repo.replace_rules(
        {
            "AAPL": {
                "symbol": "AAPL",
                "stop_loss_price": 100.0,
                "take_profit_price": 150.0,
                "updated_at": timestamp,
            }
        }
    )

    assert captured_rows, "executemany_write should be called with prepared rows"
    stored_timestamp = captured_rows[0]["timestamp"]

    assert isinstance(stored_timestamp, datetime)
    assert stored_timestamp.tzinfo is None

    expected_timestamp = timestamp.astimezone(timezone.utc).replace(tzinfo=None)
    assert stored_timestamp == expected_timestamp

    formatted = format_datetime_for_db(stored_timestamp)
    assert formatted == expected_timestamp.strftime("%Y-%m-%d %H:%M:%S")

