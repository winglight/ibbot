from __future__ import annotations

import asyncio
import json
from collections import deque
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

import pytest

from services.ib_feed import MarketDataFeedService


class DummyRedis:
    def __init__(self) -> None:
        self.published: deque[tuple[str, str]] = deque()
        self.hashes: dict[str, dict[str, str]] = {}

    async def publish(self, channel: str, payload: str) -> int:
        self.published.append((channel, payload))
        return len(self.published)

    async def hset(self, key: str, *, mapping: Mapping[str, str]) -> int:
        values = self.hashes.setdefault(key, {})
        values.update(mapping)
        return len(mapping)

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))


class RecordingPriceRepository:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    async def insert_price(self, **payload: Any) -> None:
        self.records.append(dict(payload))


class ClosablePriceRepository(RecordingPriceRepository):
    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeIBClient:
    def __init__(self) -> None:
        self._price_listeners: list[Callable[[str, Mapping[str, Any]], None]] = []
        self._dom_listeners: list[Callable[[str, Sequence[Mapping[str, Any]]], None]] = []
        self._disconnect_listeners: list[Callable[[], None]] = []
        self.connect_count = 0
        self.subscribe_calls: list[tuple[int, str]] = []
        self.connected = False

    async def connect(self) -> None:
        self.connected = True
        self.connect_count += 1

    async def disconnect(self) -> None:
        self.connected = False

    async def subscribe_market_data(self, symbol: str) -> None:
        self.subscribe_calls.append((self.connect_count, symbol))

    def add_price_listener(self, callback: Callable[[str, Mapping[str, Any]], None]) -> None:
        self._price_listeners.append(callback)

    def add_dom_listener(self, callback: Callable[[str, Sequence[Mapping[str, Any]]], None]) -> None:
        self._dom_listeners.append(callback)

    def add_disconnect_listener(self, callback: Callable[[], None]) -> None:
        self._disconnect_listeners.append(callback)

    def emit_price(self, symbol: str, data: Mapping[str, Any]) -> None:
        for listener in list(self._price_listeners):
            listener(symbol, data)

    def emit_dom(self, symbol: str, levels: Sequence[Mapping[str, Any]]) -> None:
        for listener in list(self._dom_listeners):
            listener(symbol, levels)

    def trigger_disconnect(self) -> None:
        self.connected = False
        for listener in list(self._disconnect_listeners):
            listener()


async def _run_price_update_flow() -> None:
    redis = DummyRedis()
    repository = RecordingPriceRepository()
    client = FakeIBClient()
    service = MarketDataFeedService(
        client,
        redis,
        subscriptions=["AAPL"],
        price_repository=repository,
        reconnect_delay=0.05,
        logger_name="tests.ib_feed",
        health_interval=0.05,
    )

    await service.start()
    client.emit_price(
        "AAPL",
        {
            "bid": 199.5,
            "ask": 200.5,
            "last": 200.0,
            "bid_size": 10,
            "ask_size": 12,
            "volume": 1500,
        },
    )
    await asyncio.sleep(0.1)

    assert service.price_updates == 1
    assert redis.published
    channel, payload = redis.published[-1]
    assert channel == "pubsub:ib:price:AAPL"
    data = json.loads(payload)
    assert data["symbol"] == "AAPL"
    assert pytest.approx(data["bid"], rel=1e-6) == 199.5

    snapshot = await redis.hgetall("hash:market:snapshot:AAPL")
    assert snapshot["symbol"] == "AAPL"
    assert snapshot["bid"] == "199.5"
    assert "timestamp" in snapshot

    assert repository.records
    record = repository.records[-1]
    assert record["symbol"] == "AAPL"
    assert record["bid"] == 199.5
    assert record["ask"] == 200.5
    assert isinstance(record["observed_at"], datetime)
    assert record["observed_at"] == record["recorded_at"]
    assert record["extra"]["last"] == 200.0
    assert record["extra"]["volume"] == 1500.0

    health = redis.hashes.get("hash:service:health:ib-feed") or {}
    assert health.get("status") == "running"
    assert health.get("healthy") == "true"
    assert health.get("price_updates_total") is not None

    await service.stop()
    stopped_health = redis.hashes.get("hash:service:health:ib-feed") or {}
    assert stopped_health.get("status") == "offline"
    assert stopped_health.get("healthy") == "false"


def test_price_updates_publish_and_persist() -> None:
    asyncio.run(_run_price_update_flow())


async def _run_dom_serialisation_flow() -> None:
    redis = DummyRedis()
    repository = RecordingPriceRepository()
    client = FakeIBClient()
    service = MarketDataFeedService(
        client,
        redis,
        subscriptions=["ES"],
        price_repository=repository,
        reconnect_delay=0.05,
        logger_name="tests.ib_feed",
        health_interval=0.05,
    )

    await service.start()
    client.emit_dom(
        "ES",
        [
            {"side": "bid", "level": 0, "price": 4300.25, "size": 5},
            {"side": "bid", "level": 1, "price": 4300.0, "size": 8},
            {"side": "ask", "level": 0, "price": 4300.5, "size": 7},
        ],
    )
    await asyncio.sleep(0.05)

    assert service.dom_updates == 1
    dom_snapshot = await redis.hgetall("hash:market:snapshot:ES")
    payload = json.loads(dom_snapshot["dom"])
    assert payload["symbol"] == "ES"
    assert payload["bids"][0]["level"] == 0
    assert payload["asks"][0]["price"] == 4300.5

    await service.stop()


def test_dom_updates_are_serialised() -> None:
    asyncio.run(_run_dom_serialisation_flow())


async def _run_throughput_flow() -> None:
    redis = DummyRedis()
    repository = RecordingPriceRepository()
    client = FakeIBClient()
    service = MarketDataFeedService(
        client,
        redis,
        subscriptions=["NQ"],
        price_repository=repository,
        reconnect_delay=0.01,
        logger_name="tests.ib_feed",
        health_interval=0.05,
    )

    await service.start()

    for idx in range(50):
        client.emit_price(
            "NQ",
            {
                "bid": 15000.0 + idx,
                "ask": 15000.5 + idx,
                "last": 15000.25 + idx,
                "bid_size": 1 + idx,
                "ask_size": 2 + idx,
                "volume": 1000 + idx,
            },
        )
    await asyncio.sleep(0.2)

    assert service.price_updates == 50
    assert len(repository.records) >= 50
    assert len(redis.published) >= 50

    await service.stop()
    final_health = redis.hashes.get("hash:service:health:ib-feed") or {}
    assert final_health.get("status") == "offline"


def test_service_handles_high_throughput() -> None:
    asyncio.run(_run_throughput_flow())


async def _run_reconnect_flow() -> None:
    redis = DummyRedis()
    repository = RecordingPriceRepository()
    client = FakeIBClient()
    service = MarketDataFeedService(
        client,
        redis,
        subscriptions=["CL", "GC"],
        price_repository=repository,
        reconnect_delay=0.05,
        logger_name="tests.ib_feed",
        health_interval=0.05,
    )

    await service.start()
    assert client.connect_count == 1
    assert [symbol for _, symbol in client.subscribe_calls] == ["CL", "GC"]

    client.trigger_disconnect()

    async def _wait_for_reconnect() -> None:
        while client.connect_count < 2:
            await asyncio.sleep(0.05)

    await asyncio.wait_for(_wait_for_reconnect(), timeout=2.0)
    await asyncio.sleep(0.1)

    assert client.connect_count >= 2
    # Each reconnect should re-subscribe all symbols
    subscribed_symbols = [symbol for _, symbol in client.subscribe_calls]
    assert subscribed_symbols.count("CL") >= 2
    assert subscribed_symbols.count("GC") >= 2

    await service.stop()


def test_reconnect_restores_subscriptions() -> None:
    asyncio.run(_run_reconnect_flow())


async def _run_repository_factory_flow() -> ClosablePriceRepository:
    redis = DummyRedis()
    client = FakeIBClient()
    repository = ClosablePriceRepository()

    async def factory() -> ClosablePriceRepository:
        return repository

    service = MarketDataFeedService(
        client,
        redis,
        subscriptions=[],
        price_repository_factory=factory,
        reconnect_delay=0.01,
        logger_name="tests.ib_feed",
        health_interval=0.05,
    )

    await service.start()
    assert service.price_repository is repository
    await service.stop()
    assert repository.closed
    return repository


def test_price_repository_factory_lifecycle() -> None:
    repository = asyncio.run(_run_repository_factory_flow())
    assert repository.closed
