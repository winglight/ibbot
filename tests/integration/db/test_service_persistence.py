import asyncio
from typing import Mapping

import pytest

from src.data_layer.event_bus import EventEnvelope
from src.data_layer.persistence import MariaDbPersistenceBackend


@pytest.mark.asyncio
async def test_mariadb_persistence_backend_records_events_and_metrics(
    sqlite_db,
    market_event_repository_factory,
) -> None:
    recorded: list[dict[str, object]] = []

    class DummyMetricsWriter:
        async def record_metrics(self, metrics):
            if isinstance(metrics, Mapping):
                recorded.append(dict(metrics))
            else:
                recorded.extend(dict(entry) for entry in metrics)

    backend = MariaDbPersistenceBackend(
        repository_factory=market_event_repository_factory,
        metric_writer=DummyMetricsWriter(),
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
    assert tuple(event_row) == ("market.candle", "candles.AAPL")
    assert recorded[0]["service"] == "market-data"
    assert recorded[0]["metric"] == "events-written"
    assert recorded[0]["value"] == 1
