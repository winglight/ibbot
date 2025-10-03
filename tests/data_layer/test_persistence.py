import asyncio
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

import pytest

from src.data_layer.event_bus import EventEnvelope
from src.data_layer.persistence import MariaDbPersistenceBackend

try:  # pragma: no cover - optional dependency
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from src.ibbot.db.schemas import Base, MarketEvent
except ImportError:  # pragma: no cover - optional dependency
    AsyncSession = Any  # type: ignore[assignment]
    async_sessionmaker = Any  # type: ignore[assignment]
    create_async_engine = None  # type: ignore[assignment]
    select = None  # type: ignore[assignment]
    Base = None  # type: ignore[assignment]
    MarketEvent = None  # type: ignore[assignment]
    SQLALCHEMY_AVAILABLE = False
else:
    SQLALCHEMY_AVAILABLE = True


class _StubRepository:
    def __init__(self) -> None:
        self.events: list[EventEnvelope] = []

    async def insert_event(self, event: EventEnvelope) -> None:
        self.events.append(event)


class _StubMetricRepository:
    def __init__(self) -> None:
        self.payloads: list[list[Mapping[str, Any]]] = []

    async def record_metrics(
        self, metrics: Mapping[str, Any] | Iterable[Mapping[str, Any]]
    ) -> None:
        if isinstance(metrics, Mapping):
            self.payloads.append([dict(metrics)])
        else:
            self.payloads.append([dict(entry) for entry in metrics])


def test_mariadb_backend_uses_repository_directly() -> None:
    repo = _StubRepository()
    backend = MariaDbPersistenceBackend(repository=repo)
    envelope = EventEnvelope(
        event_type="market.tick",
        topic="market.tick",
        symbol="EURUSD",
        source="ibkr",
        ts_ns=123,
        payload={"price": 1.2345},
        meta={"channel": "market.tick"},
    )

    backend.write_event(envelope)

    assert repo.events and repo.events[0].topic == "market.tick"
    assert repo.events[0].payload["price"] == 1.2345


def test_mariadb_backend_uses_metric_repository_directly() -> None:
    repo = _StubRepository()
    metric_repo = _StubMetricRepository()
    backend = MariaDbPersistenceBackend(repository=repo, metric_repository=metric_repo)

    backend.record_service_metrics({"service": "test", "metric": "throughput", "value": 1})

    assert metric_repo.payloads == [[{"service": "test", "metric": "throughput", "value": 1}]]


@pytest.fixture
def sqlite_session_factory():
    if not SQLALCHEMY_AVAILABLE:
        pytest.skip("SQLAlchemy is not installed")

    async def _create_engine():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        return engine

    engine = asyncio.run(_create_engine())
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)

    yield factory

    async def _dispose():
        await engine.dispose()

    asyncio.run(_dispose())


def _fetch_events(factory: Any):
    async def _query():
        async with factory() as session:
            result = await session.execute(select(MarketEvent))
            return result.scalars().all()

    return asyncio.run(_query())


@pytest.mark.skipif(not SQLALCHEMY_AVAILABLE, reason="SQLAlchemy is not installed")
def test_mariadb_backend_persists_event_without_running_loop(sqlite_session_factory):
    backend = MariaDbPersistenceBackend(session_factory=sqlite_session_factory)
    ts_ns = 1_700_000_000_123_456_789
    envelope = EventEnvelope(
        event_type="market.tick",
        topic="market.tick",
        symbol="AAPL",
        source="ibkr",
        ts_ns=ts_ns,
        payload={"price": 123.45},
        meta={"channel": "market.tick"},
    )

    backend.write_event(envelope)

    events = _fetch_events(sqlite_session_factory)
    assert len(events) == 1
    event = events[0]
    assert event.event_type == "market.tick"
    assert event.topic == "market.tick"
    assert event.symbol == "AAPL"
    assert event.source == "ibkr"
    assert event.payload["price"] == 123.45
    assert event.meta["channel"] == "market.tick"
    expected_time = datetime.fromtimestamp(ts_ns / 1_000_000_000, tz=timezone.utc)
    assert event.event_time == expected_time


@pytest.mark.skipif(not SQLALCHEMY_AVAILABLE, reason="SQLAlchemy is not installed")
def test_mariadb_backend_schedules_when_loop_running(sqlite_session_factory):
    backend = MariaDbPersistenceBackend(session_factory=sqlite_session_factory)
    ts_ns = 1_800_000_000_987_654_321
    envelope = EventEnvelope(
        event_type="market.candle",
        topic="market.candle.1m",
        symbol="MSFT",
        source="ibkr",
        ts_ns=ts_ns,
        payload={"close": 456.78},
        meta={"interval": "1m"},
    )

    async def _run() -> None:
        backend.write_event(envelope)
        await asyncio.sleep(0)

    asyncio.run(_run())

    events = _fetch_events(sqlite_session_factory)
    assert len(events) == 1
    event = events[0]
    assert event.topic == "market.candle.1m"
    assert event.payload["close"] == 456.78
    expected_time = datetime.fromtimestamp(ts_ns / 1_000_000_000, tz=timezone.utc)
    assert event.event_time == expected_time
