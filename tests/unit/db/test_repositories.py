from __future__ import annotations

from datetime import datetime, timezone

import pytest


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

