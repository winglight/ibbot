import asyncio
import inspect
import json
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Mapping

import pytest

from services.market_data.runtime import MarketDataRuntime
from services.market_data.subscriptions import PubSubListener
from src.configuration.provider import MarketDataServiceConfig


pytestmark = pytest.mark.anyio("asyncio")


class FakePubSub:
    def __init__(self, fail_once: bool = False) -> None:
        self.queue: asyncio.Queue[Mapping[str, Any] | None] = asyncio.Queue()
        self.channels: set[str] = set()
        self._fail_once = fail_once
        self.closed = False

    async def subscribe(self, *channels: str) -> None:
        self.channels.update(channels)

    async def unsubscribe(self, *channels: str) -> None:
        for channel in channels:
            self.channels.discard(channel)

    async def close(self) -> None:
        self.closed = True
        await self.queue.put(None)

    async def listen(self):  # pragma: no cover - generator semantics exercised indirectly
        if self._fail_once:
            self._fail_once = False
            raise ConnectionError("disconnected")
        while not self.closed:
            message = await self.queue.get()
            if message is None:
                break
            yield message


class FakeRedis:
    def __init__(self) -> None:
        self.fail_first_listen = False
        self.pubsubs: list[FakePubSub] = []
        self.hashes: dict[str, Mapping[str, Any]] = {}

    def pubsub(self, ignore_subscribe_messages: bool = True) -> FakePubSub:  # noqa: ARG002
        pubsub = FakePubSub(fail_once=self.fail_first_listen)
        self.fail_first_listen = False
        self.pubsubs.append(pubsub)
        return pubsub

    async def hgetall(self, key: str) -> Mapping[str, Any]:
        return self.hashes.get(key, {})


class StubProvider:
    def __init__(self, config: MarketDataServiceConfig) -> None:
        self._config = config
        self._callbacks: list[Callable[[MarketDataServiceConfig], Awaitable[None] | None]] = []

    async def start(self) -> None:  # pragma: no cover - not used in tests
        return None

    async def stop(self) -> None:  # pragma: no cover - not used in tests
        return None

    async def get_config(self) -> MarketDataServiceConfig:
        return self._config

    def register_callback(self, callback: Callable[[MarketDataServiceConfig], Awaitable[None] | None]) -> None:
        self._callbacks.append(callback)

    def unregister_callback(self, callback: Callable[[MarketDataServiceConfig], Awaitable[None] | None]) -> None:
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    async def update_symbols(self, symbols: Iterable[str]) -> None:
        self._config = MarketDataServiceConfig(symbols=list(symbols))
        for callback in list(self._callbacks):
            result = callback(self._config)
            if inspect.isawaitable(result):
                await result


async def wait_for(condition: Callable[[], bool], timeout: float = 1.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


async def test_listener_recovers_from_disconnect_and_updates_snapshots() -> None:
    config = MarketDataServiceConfig(symbols=["BTCUSDT"])
    provider = StubProvider(config)
    redis = FakeRedis()
    redis.fail_first_listen = True
    runtime = MarketDataRuntime()
    runtime._update_account_snapshot(
        account_info=None,
        positions=None,
        updated_at=None,
        stale=False,
        stale_reason=None,
    )

    listener = PubSubListener(
        config_provider=provider,
        redis_client=redis,
        runtime=runtime,
        reconnect_delay=0.01,
    )

    await listener.start()

    await wait_for(lambda: len(redis.pubsubs) >= 2)
    pubsub = redis.pubsubs[-1]

    timestamp = datetime.now(timezone.utc).timestamp()
    message = {
        "type": "message",
        "channel": "pubsub:ib:price:BTCUSDT",
        "data": json.dumps({"price": 101.5, "timestamp": timestamp}),
    }
    await pubsub.queue.put(message)

    await wait_for(lambda: runtime._snapshots.get("BTCUSDT", {}).get("price") == pytest.approx(101.5))

    snapshot = runtime._snapshots["BTCUSDT"]
    assert snapshot["price"] == pytest.approx(101.5)
    assert snapshot["updated_at"] is not None
    assert snapshot["latency_ms"] is not None
    assert snapshot["latency_ms"] >= 0

    await listener.stop()


async def test_listener_updates_channels_and_loads_snapshots() -> None:
    initial_config = MarketDataServiceConfig(symbols=["BTCUSDT"])
    provider = StubProvider(initial_config)
    redis = FakeRedis()
    redis.hashes["hash:market:snapshot:BTCUSDT"] = {
        "price": "123.4",
        "updated_at": "2024-01-01T00:00:00+00:00",
        "latency_ms": "5",
    }
    redis.hashes["hash:market:snapshot:ETHUSDT"] = {
        "price": "2000",
        "dom": json.dumps({"bids": [[1, 2]], "asks": [[3, 4]]}),
        "updated_at": "2024-01-01T00:00:00+00:00",
        "latency_ms": "10",
    }

    runtime = MarketDataRuntime()
    runtime._update_account_snapshot(
        account_info=None,
        positions=None,
        updated_at=None,
        stale=False,
        stale_reason=None,
    )
    listener = PubSubListener(
        config_provider=provider,
        redis_client=redis,
        runtime=runtime,
        reconnect_delay=0.01,
    )

    await listener.start()

    await wait_for(lambda: runtime._snapshots.get("BTCUSDT", {}).get("price") == pytest.approx(123.4))

    await provider.update_symbols(["BTCUSDT", "ETHUSDT"])
    await wait_for(lambda: "pubsub:ib:price:ETHUSDT" in listener._current_channels)

    eth_snapshot = runtime._snapshots.get("ETHUSDT")
    assert eth_snapshot is not None
    assert eth_snapshot["price"] == pytest.approx(2000)
    assert eth_snapshot["dom"] == {"bids": [[1, 2]], "asks": [[3, 4]]}

    pubsub = redis.pubsubs[-1]
    dom_message = {
        "type": "message",
        "channel": "pubsub:ib:dom:ETHUSDT",
        "data": json.dumps({"dom": {"bids": [[5, 6]]}, "ts": datetime.now(timezone.utc).timestamp()}),
    }
    await pubsub.queue.put(dom_message)

    await wait_for(lambda: runtime._snapshots["ETHUSDT"]["dom"] == {"bids": [[5, 6]]})

    await provider.update_symbols(["ETHUSDT"])
    await wait_for(lambda: "pubsub:ib:price:BTCUSDT" not in listener._current_channels)

    await listener.stop()
