from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from src.ibbot.db.repositories.events import MarketEventRepository
from src.ibbot.db.repositories.metrics import ServiceMetricRepository
from src.ibbot.db.repositories.order import MariaDbOrderRepository
from src.ibbot.db.schemas import MarketEvent, Order, ServiceMetric


@pytest.mark.asyncio
async def test_order_repository_upserts_payload(db_engine, db_session_factory) -> None:
    payload = {
        "order_id": "1001",
        "symbol": "AAPL",
        "side": "BUY",
        "quantity": 10,
        "price": 150.0,
        "status": "submitted",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    async with db_session_factory() as session:
        repo = MariaDbOrderRepository(session)
        await repo.upsert_order(payload)

    updated_payload = dict(payload, status="filled", filled_quantity=10, price=151.25)
    async with db_session_factory() as session:
        repo = MariaDbOrderRepository(session)
        await repo.upsert_order(updated_payload)

    async with db_session_factory() as session:
        result = await session.execute(select(Order))
        row = result.scalar_one()
        assert row.order_id == "1001"
        assert row.status == "filled"
        assert row.filled_quantity == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_order_repository_rejects_invalid_payload(db_engine, db_session_factory) -> None:
    async with db_session_factory() as session:
        repo = MariaDbOrderRepository(session)
        with pytest.raises(ValueError):
            await repo.upsert_order({"symbol": "AAPL"})


@pytest.mark.asyncio
async def test_market_event_repository_inserts_event(db_engine, db_session_factory) -> None:
    event = {
        "event_type": "market.quote",
        "topic": "quotes.AAPL",
        "symbol": "AAPL",
        "source": "unit-test",
        "payload": {"bid": 100.5, "ask": 100.6},
        "meta": {"interval": "1s"},
        "ts_ns": 1_700_000_000_000_000_000,
    }

    async with db_session_factory() as session:
        repo = MarketEventRepository(session)
        await repo.insert_event(event)

    async with db_session_factory() as session:
        result = await session.execute(select(MarketEvent))
        row = result.scalar_one()
        assert row.event_type == "market.quote"
        assert row.topic == "quotes.AAPL"
        assert row.symbol == "AAPL"
        assert row.ts_ns == 1_700_000_000_000_000_000


@pytest.mark.asyncio
async def test_service_metric_repository_records_metrics(db_engine, db_session_factory) -> None:
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

    async with db_session_factory() as session:
        repo = ServiceMetricRepository(session)
        await repo.record_metrics(metrics)

    async with db_session_factory() as session:
        result = await session.execute(select(ServiceMetric))
        rows = result.scalars().all()
        assert len(rows) == 2
        names = {row.metric for row in rows}
        assert names == {"latency", "throughput"}


@pytest.mark.asyncio
async def test_service_metric_repository_validates_input(db_engine, db_session_factory) -> None:
    async with db_session_factory() as session:
        repo = ServiceMetricRepository(session)
        with pytest.raises(ValueError):
            await repo.record_metrics({"service": "", "metric": "", "value": None})
