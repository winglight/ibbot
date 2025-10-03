import asyncio
from dataclasses import dataclass

import pytest

from src.data_layer import persistence
from src.data_layer.event_bus import EventEnvelope


@dataclass
class _FakeSession:
    closed: bool = False

    async def close(self) -> None:
        self.closed = True


class _MarketEventRecorder:
    def __init__(self, sink: list[dict]) -> None:
        self._sink = sink

    async def insert_event(self, payload: dict) -> None:
        self._sink.append(payload)


class _MetricRecorder:
    def __init__(self, sink: list[dict]) -> None:
        self._sink = sink

    async def record_metrics(self, payload) -> None:
        if isinstance(payload, dict):
            self._sink.append(payload)
        else:
            self._sink.extend(list(payload))


def test_database_backend_persists_market_events(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[dict] = []
    sessions: list[_FakeSession] = []

    def session_factory() -> _FakeSession:
        session = _FakeSession()
        sessions.append(session)
        return session

    monkeypatch.setattr(
        persistence,
        "market_event_repository",
        lambda session: _MarketEventRecorder(events),
    )

    backend = persistence.DatabasePersistenceBackend(session_factory=session_factory)
    envelope = EventEnvelope(
        event_type="market.tick",
        topic="market.price",
        symbol="SYM",
        source="feed",
        ts_ns=123,
        payload={"bid": 1.23},
        meta={"source": "test"},
    )

    backend.write_event(envelope)

    assert events, "Expected market event to be recorded"
    row = events[0]
    assert row["event_type"] == "market.tick"
    assert row["topic"] == "market.price"
    assert row["payload"] == {"bid": 1.23}
    assert sessions and sessions[0].closed is True


def test_database_backend_persists_service_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    metrics: list[dict] = []

    def session_factory() -> _FakeSession:
        return _FakeSession()

    monkeypatch.setattr(
        persistence,
        "service_metric_repository",
        lambda session: _MetricRecorder(metrics),
    )

    backend = persistence.DatabasePersistenceBackend(session_factory=session_factory)
    envelope = EventEnvelope(
        event_type="service.metric",
        topic="metrics",
        payload={"service": "queue", "metric": "depth", "value": 5},
    )

    backend.write_event(envelope)

    assert metrics == [{"service": "queue", "metric": "depth", "value": 5}]


def test_database_backend_handles_async_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    metrics: list[dict] = []

    def session_factory() -> _FakeSession:
        return _FakeSession()

    monkeypatch.setattr(
        persistence,
        "service_metric_repository",
        lambda session: _MetricRecorder(metrics),
    )

    backend = persistence.DatabasePersistenceBackend(session_factory=session_factory)

    async def scenario() -> None:
        envelope = EventEnvelope(
            event_type="service.metric",
            topic="metrics",
            payload=[{"service": "queue", "metric": "latency", "value": 1.2}],
        )
        backend.write_event(envelope)
        # Give the scheduled task a chance to run.
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert metrics == [
        {"service": "queue", "metric": "latency", "value": 1.2},
    ]
