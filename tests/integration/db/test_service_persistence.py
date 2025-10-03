from __future__ import annotations

import asyncio
import pytest
from sqlalchemy import select

from src.data_layer.event_bus import EventEnvelope
from src.data_layer.persistence import MariaDbPersistenceBackend
from src.db import run_sql_query
from src.ibbot.db.repositories.metrics import ServiceMetricRepository
from src.ibbot.db.schemas import MarketEvent, ServiceMetric
from services.market_data.main import MarketDataRuntime
from services.queue_monitor.main import QueueMonitorRuntime
from services.optimizer.main import OptimizerService


@pytest.mark.asyncio
async def test_mariadb_persistence_backend_records_events_and_metrics(
    db_engine, db_session_factory
) -> None:
    backend = MariaDbPersistenceBackend(
        session_factory=db_session_factory,
        metric_session_factory=db_session_factory,
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

    async with db_session_factory() as session:
        events = await session.execute(select(MarketEvent))
        event_row = events.scalar_one()
        assert event_row.event_type == "market.candle"
        assert event_row.topic == "candles.AAPL"

        metrics = await session.execute(select(ServiceMetric))
        metric_row = metrics.scalar_one()
        assert metric_row.metric == "events-written"
        assert metric_row.service == "market-data"


@pytest.mark.asyncio
async def test_queue_monitor_runtime_persists_metrics(db_engine, db_session_factory) -> None:
    runtime = QueueMonitorRuntime(
        queues=[{"name": "commands", "queue_key": "queue:commands"}],
        stream_targets=(),
        metrics_repository_factory=lambda: ServiceMetricRepository(db_session_factory()),
        pubsub_channel=None,
        service_name="queue-monitor",
    )

    await runtime._initialize_metrics_repository()
    queue_metrics = [
        {
            "name": "commands",
            "queue_key": "queue:commands",
            "length": 5,
            "consumption_rate": 2.5,
            "stale_seconds": 0.5,
        }
    ]
    await runtime._write_service_metrics(queue_metrics, [])
    await runtime._close_metrics_repository()

    async with db_session_factory() as session:
        rows = await session.execute(select(ServiceMetric))
        metrics = rows.scalars().all()
        assert len(metrics) == 3
        names = {metric.metric for metric in metrics}
        assert names == {
            "commands-queue-length",
            "commands-consumption-rate",
            "commands-stale-seconds",
        }


@pytest.mark.asyncio
async def test_market_data_runtime_flushes_metrics(db_engine, db_session_factory) -> None:
    runtime = MarketDataRuntime(
        metrics_interval=0.1,
        service_name="market-data",
        metrics_repository_factory=lambda: ServiceMetricRepository(db_session_factory()),
    )

    await runtime._initialize_metrics_repository()
    runtime._metrics_counts["price"] = 8
    runtime._metrics_latency["price"] = 0.4
    runtime._metrics_latency_max["price"] = 0.25
    await runtime._flush_metrics(final=True)
    await runtime._close_metrics_repository()

    async with db_session_factory() as session:
        result = await session.execute(select(ServiceMetric))
        metrics = result.scalars().all()
        assert any(metric.metric == "price-throughput" for metric in metrics)
        assert any(metric.metric == "price-latency" for metric in metrics)


@pytest.mark.asyncio
async def test_optimizer_service_reads_training_data(db_engine, db_session_factory) -> None:
    async with db_session_factory() as session:
        repo = ServiceMetricRepository(session)
        await repo.record_metrics(
            {
                "service": "optimizer",
                "metric": "training-value",
                "value": 42.0,
            }
        )

    service = OptimizerService(db_query=run_sql_query)
    data = await service._query_database("SELECT metric, value FROM service_metrics")
    assert any(row["metric"] == "training-value" for row in data)
