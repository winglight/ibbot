from datetime import datetime, timezone

import pytest

@pytest.mark.asyncio
async def test_order_repository_upserts_payload(
    sqlite_db, order_repository_factory
) -> None:
    repo = await order_repository_factory()
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

    row = sqlite_db.execute(
        "SELECT status, filled_quantity FROM orders WHERE order_id = ?", ("1001",)
    ).fetchone()
    assert row["status"] == "filled"
    assert row["filled_quantity"] == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_market_event_repository_inserts_event(
    sqlite_db, market_event_repository_factory
) -> None:
    repo = await market_event_repository_factory()
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

    row = sqlite_db.execute(
        "SELECT event_type, topic, symbol, ts_ns FROM market_events"
    ).fetchone()
    assert tuple(row) == ("market.quote", "quotes.AAPL", "AAPL", 1_700_000_000_000_000_000)


@pytest.mark.asyncio
async def test_service_metric_repository_records_metrics(
    sqlite_db, service_metric_repository_factory
) -> None:
    repo = await service_metric_repository_factory()
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

    rows = sqlite_db.execute("SELECT metric, value FROM service_metrics").fetchall()
    assert {row["metric"] for row in rows} == {"latency", "throughput"}
