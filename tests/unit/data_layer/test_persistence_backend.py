import asyncio
import threading

import pytest

from src.data_layer.event_bus import EventEnvelope
from src.data_layer.persistence import MariaDbPersistenceBackend


class _SyncEventRepository:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def insert_event(self, envelope: EventEnvelope) -> None:  # pragma: no cover - exercised via thread
        self.calls.append(threading.get_ident())


class _SyncMetricRepository:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def record_metrics(self, metrics: dict[str, object]) -> None:  # pragma: no cover - exercised via thread
        self.calls.append(threading.get_ident())


class _AsyncEventRepository:
    def __init__(self) -> None:
        self.calls: list[int] = []

    async def insert_event(self, envelope: EventEnvelope) -> None:
        self.calls.append(threading.get_ident())


class _AsyncMetricRepository:
    def __init__(self) -> None:
        self.calls: list[int] = []

    async def record_metrics(self, metrics: dict[str, object]) -> None:
        self.calls.append(threading.get_ident())


@pytest.mark.asyncio
async def test_mariadb_backend_offloads_sync_repositories() -> None:
    event_repo = _SyncEventRepository()
    metric_repo = _SyncMetricRepository()
    backend = MariaDbPersistenceBackend(
        repository=event_repo,
        metric_repository=metric_repo,
    )

    envelope = EventEnvelope(event_type="test", topic="events.test")
    backend.write_event(envelope)
    backend.record_service_metrics({"service": "svc", "metric": "calls", "value": 1})

    await asyncio.sleep(0.05)

    main_thread = threading.get_ident()
    assert event_repo.calls and all(thread_id != main_thread for thread_id in event_repo.calls)
    assert metric_repo.calls and all(thread_id != main_thread for thread_id in metric_repo.calls)


@pytest.mark.asyncio
async def test_mariadb_backend_handles_async_repositories() -> None:
    event_repo = _AsyncEventRepository()
    metric_repo = _AsyncMetricRepository()
    backend = MariaDbPersistenceBackend(
        repository=event_repo,
        metric_repository=metric_repo,
    )

    envelope = EventEnvelope(event_type="test", topic="events.test")
    backend.write_event(envelope)
    backend.record_service_metrics({"service": "svc", "metric": "calls", "value": 1})

    await asyncio.sleep(0.05)

    main_thread = threading.get_ident()
    assert event_repo.calls == [main_thread]
    assert metric_repo.calls == [main_thread]
