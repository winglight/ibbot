import asyncio
from typing import Any, Dict

import pytest

from src.configuration.provider import ConfigProvider, MarketDataServiceConfig


class FakePubSub:
    def __init__(self) -> None:
        self.queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue()
        self.channel: str | None = None
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self.channel = channel

    async def unsubscribe(self, channel: str) -> None:  # pragma: no cover - cleanup path
        if self.channel == channel:
            self.channel = None

    async def close(self) -> None:
        self.closed = True

    async def publish(self, message: Any) -> None:
        await self.queue.put({"type": "message", "data": message, "channel": self.channel})

    async def listen(self):
        while True:
            message = await self.queue.get()
            yield message


class FakeRedis:
    def __init__(self, initial: Dict[str, Dict[str, Any]] | None = None) -> None:
        self.storage = initial or {}
        self.pubsub_instance = FakePubSub()

    async def hgetall(self, key: str) -> Dict[str, Any]:
        return dict(self.storage.get(key, {}))

    def pubsub(self, ignore_subscribe_messages: bool = True) -> FakePubSub:
        return self.pubsub_instance


@pytest.mark.asyncio
async def test_initial_load_uses_env_and_redis(monkeypatch):
    fake = FakeRedis({"config:market-data": {"stream_length": "2048"}})

    async def factory():
        return fake

    monkeypatch.setenv("MARKET_DATA_SYMBOLS", "BTCUSDT, ETHUSDT")
    monkeypatch.setenv("MARKET_DATA_INDICATOR_PERIOD", "30")

    provider: ConfigProvider[MarketDataServiceConfig] = ConfigProvider(
        "market-data", MarketDataServiceConfig, redis_factory=factory
    )
    config = await provider.refresh()

    assert config.symbols == ["BTCUSDT", "ETHUSDT"]
    assert config.stream_length == 2048
    assert config.indicator_period == 30


@pytest.mark.asyncio
async def test_pubsub_notification_triggers_callback(monkeypatch):
    fake = FakeRedis({"config:market-data": {"stream_length": "512"}})

    async def factory():
        return fake

    provider: ConfigProvider[MarketDataServiceConfig] = ConfigProvider(
        "market-data", MarketDataServiceConfig, redis_factory=factory
    )

    updates: list[MarketDataServiceConfig] = []
    event = asyncio.Event()

    def callback(config: MarketDataServiceConfig) -> None:
        updates.append(config)
        event.set()

    provider.register_callback(callback)

    await provider.start()
    assert updates[-1].stream_length == 512

    await fake.pubsub_instance.publish("market-data")
    await asyncio.wait_for(event.wait(), timeout=1)

    # ensure refresh triggered again
    assert len(updates) >= 2
    await provider.stop()


@pytest.mark.asyncio
async def test_refresh_falls_back_without_redis(monkeypatch):
    async def factory():
        return None

    monkeypatch.delenv("MARKET_DATA_SYMBOLS", raising=False)

    provider: ConfigProvider[MarketDataServiceConfig] = ConfigProvider(
        "market-data", MarketDataServiceConfig, redis_factory=factory
    )

    config = await provider.refresh()

    assert config.symbols == ["BTCUSDT"]
    assert config.stream_length == 1000
    assert config.indicator_period == 14
