import threading
from typing import Any, Iterable, Mapping

import pytest

from src.services.factories import (
    MarketPriceRepositoryAdapter,
    ServiceMetricsWriterAdapter,
    create_market_price_repository,
    create_service_metrics_writer,
)


class SyncMetricsWriter:
    def __init__(self) -> None:
        self.calls: list[tuple[int, Mapping[str, Any]]] = []
        self.closed = False

    def record_metrics(self, metrics: Mapping[str, Any] | Iterable[Mapping[str, Any]]) -> None:
        if isinstance(metrics, Mapping):
            payloads = [dict(metrics)]
        else:
            payloads = [dict(entry) for entry in metrics]
        for entry in payloads:
            self.calls.append((threading.get_ident(), entry))

    def close(self) -> None:
        self.closed = True


class AsyncMetricsWriter:
    def __init__(self) -> None:
        self.thread_ids: list[int] = []
        self.closed = False

    async def record_metrics(
        self, metrics: Mapping[str, Any] | Iterable[Mapping[str, Any]]
    ) -> None:
        self.thread_ids.append(threading.get_ident())

    async def close(self) -> None:
        self.closed = True


class SyncPriceRepository:
    def __init__(self) -> None:
        self.calls: list[int] = []
        self.closed = False

    def insert_price(self, **payload: Any) -> None:
        self.calls.append(threading.get_ident())

    def close(self) -> None:
        self.closed = True


class AsyncPriceRepository:
    def __init__(self) -> None:
        self.thread_ids: list[int] = []
        self.closed = False

    async def insert_price(self, **payload: Any) -> None:
        self.thread_ids.append(threading.get_ident())

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_create_service_metrics_writer_wraps_sync() -> None:
    writer = SyncMetricsWriter()
    main_thread = threading.get_ident()

    adapter = await create_service_metrics_writer(factory=lambda: writer)
    assert isinstance(adapter, ServiceMetricsWriterAdapter)
    await adapter.record_metrics({"service": "svc", "metric": "calls", "value": 1})
    await adapter.aclose()

    assert writer.calls
    for thread_id, _ in writer.calls:
        assert thread_id != main_thread
    assert writer.closed


@pytest.mark.asyncio
async def test_create_service_metrics_writer_keeps_async_writer_on_loop() -> None:
    writer = AsyncMetricsWriter()
    main_thread = threading.get_ident()

    adapter = await create_service_metrics_writer(factory=lambda: writer)
    assert isinstance(adapter, ServiceMetricsWriterAdapter)
    await adapter.record_metrics({"service": "svc", "metric": "calls", "value": 2})
    await adapter.aclose()

    assert writer.thread_ids == [main_thread]
    assert writer.closed


@pytest.mark.asyncio
async def test_create_market_price_repository_wraps_sync() -> None:
    repository = SyncPriceRepository()
    main_thread = threading.get_ident()

    adapter = await create_market_price_repository(factory=lambda: repository)
    assert isinstance(adapter, MarketPriceRepositoryAdapter)
    await adapter.insert_price(symbol="AAPL", bid=1.0, ask=1.1)
    await adapter.aclose()

    assert repository.calls
    for thread_id in repository.calls:
        assert thread_id != main_thread
    assert repository.closed


@pytest.mark.asyncio
async def test_create_market_price_repository_keeps_async_repo_on_loop() -> None:
    repository = AsyncPriceRepository()
    main_thread = threading.get_ident()

    adapter = await create_market_price_repository(factory=lambda: repository)
    assert isinstance(adapter, MarketPriceRepositoryAdapter)
    await adapter.insert_price(symbol="ES", bid=4300.0, ask=4300.5)
    await adapter.aclose()

    assert repository.thread_ids == [main_thread]
    assert repository.closed
