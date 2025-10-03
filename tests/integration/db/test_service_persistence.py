import asyncio
import pytest

from src.data_layer.event_bus import EventEnvelope
from src.data_layer.persistence import MariaDbPersistenceBackend


@pytest.mark.asyncio
async def test_mariadb_persistence_backend_records_events_and_metrics(
    sqlite_db,
    market_event_repository_factory,
    service_metric_repository_factory,
) -> None:
    backend = MariaDbPersistenceBackend(
        repository_factory=market_event_repository_factory,
        metric_repository_factory=service_metric_repository_factory,
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

    event_row = sqlite_db.execute(
        "SELECT event_type, topic FROM market_events"
    ).fetchone()
    metric_row = sqlite_db.execute(
        "SELECT service, metric, value FROM service_metrics"
    ).fetchone()

    assert tuple(event_row) == ("market.candle", "candles.AAPL")
    assert tuple(metric_row) == ("market-data", "events-written", 1.0)
