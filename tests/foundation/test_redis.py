from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any, Callable

import pytest

from src.ibbot import redis as redis_module
from src.ibbot.redis import (
    LegacyIBCommandUnavailableError,
    PubSubListener,
    StreamMessage,
    ack_stream,
    enqueue_ib_command,
    get_queue_length,
    get_stream_metrics,
    publish_market_update,
    read_stream,
    redis_get,
    redis_set,
)


@pytest.fixture
def loop() -> asyncio.AbstractEventLoop:
    loop = asyncio.new_event_loop()
    yield loop
    loop.run_until_complete(loop.shutdown_asyncgens())
    loop.close()


@pytest.fixture
def run(loop: asyncio.AbstractEventLoop) -> Callable[[Any], Any]:
    def _run(coro: Any) -> Any:
        return loop.run_until_complete(coro)

    return _run


@pytest.fixture
def redis_client(loop: asyncio.AbstractEventLoop) -> "fakeredis.aioredis.FakeRedis":  # type: ignore[name-defined]
    fakeredis = pytest.importorskip("fakeredis.aioredis")
    client = fakeredis.FakeRedis(decode_responses=True)
    loop.run_until_complete(client.flushall())
    try:
        yield client
    finally:
        loop.run_until_complete(client.flushall())
        loop.run_until_complete(client.close())


def test_get_redis_client_delegates_to_cache(run: Callable[[Any], Any], monkeypatch: pytest.MonkeyPatch) -> None:
    if redis_module.Redis is None:
        pytest.skip("redis.asyncio not available")

    sentinel = object()

    async def fake_ensure() -> Any:
        return sentinel

    monkeypatch.setattr(redis_module, "cache_ensure_redis", fake_ensure)
    result = run(redis_module.get_redis_client())
    assert result is sentinel


def test_get_redis_pool_uses_shared_client(run: Callable[[Any], Any], monkeypatch: pytest.MonkeyPatch) -> None:
    if redis_module.Redis is None:
        pytest.skip("redis.asyncio not available")

    class DummyPool:
        pass

    class DummyClient:
        def __init__(self) -> None:
            self.connection_pool = DummyPool()

    async def fake_ensure() -> Any:
        return DummyClient()

    monkeypatch.setattr(redis_module, "cache_ensure_redis", fake_ensure)
    monkeypatch.setattr(redis_module, "ConnectionPool", DummyPool)

    pool = run(redis_module.get_redis_pool())
    assert isinstance(pool, DummyPool)


def test_close_global_client_delegates_to_cache(run: Callable[[Any], Any], monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[None] = []

    async def fake_close() -> None:
        calls.append(None)

    monkeypatch.setattr(redis_module, "cache_close_redis", fake_close)
    run(redis_module.close_global_client())
    assert calls


def test_basic_get_set_and_queue(
    run: Callable[[Any], Any],
    redis_client: "fakeredis.aioredis.FakeRedis",  # type: ignore[name-defined]
) -> None:
    assert run(redis_set("demo:key", "value", redis=redis_client, expire=1))
    assert run(redis_get("demo:key", redis=redis_client)) == "value"

    queue_key = "queue:test"
    payload = {"command_id": "cmd-1", "action": "ping"}
    with pytest.raises(LegacyIBCommandUnavailableError):
        run(enqueue_ib_command(payload, redis=redis_client, queue_key=queue_key))
    assert run(get_queue_length(queue_key, redis=redis_client)) == 0


def test_stream_read_ack_and_metrics(
    run: Callable[[Any], Any],
    redis_client: "fakeredis.aioredis.FakeRedis",  # type: ignore[name-defined]
) -> None:
    stream_key = "stream:test"
    group = "group:test"
    consumer = "consumer-1"

    run(redis_client.xgroup_create(stream_key, group, id="0-0", mkstream=True))
    message_id = run(redis_client.xadd(stream_key, {"price": "100"}))

    messages = run(
        read_stream(
            stream_key=stream_key,
            group=group,
            consumer=consumer,
            redis=redis_client,
            block=10,
        )
    )
    assert [msg.message_id for msg in messages] == [message_id]
    assert all(isinstance(msg, StreamMessage) for msg in messages)

    metrics = run(get_stream_metrics(stream_key=stream_key, group=group, redis=redis_client))
    assert metrics["pending"] == 1
    assert metrics["stale_ms"] >= 0

    acked = run(ack_stream([message_id], stream_key=stream_key, group=group, redis=redis_client))
    assert acked == 1

    metrics_after = run(get_stream_metrics(stream_key=stream_key, group=group, redis=redis_client))
    assert metrics_after == {"pending": 0, "stale_ms": 0}


def test_publish_market_update(
    run: Callable[[Any], Any],
    redis_client: "fakeredis.aioredis.FakeRedis",  # type: ignore[name-defined]
) -> None:
    channel = "pubsub:test"
    pubsub = redis_client.pubsub()
    run(pubsub.subscribe(channel))

    receivers = run(publish_market_update({"symbol": "AAPL"}, redis=redis_client, channel=channel))
    assert receivers == 1

    message = run(pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0))
    assert message is not None
    assert json.loads(message["data"]) == {"symbol": "AAPL"}

    run(pubsub.unsubscribe(channel))
    run(pubsub.close())


class _MemoryBroker:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[dict[str, str]]]] = defaultdict(list)

    def register(self, channel: str, queue: asyncio.Queue[dict[str, str]]) -> None:
        self._subscribers[channel].append(queue)

    def unregister(self, channel: str, queue: asyncio.Queue[dict[str, str]]) -> None:
        subscribers = self._subscribers.get(channel, [])
        if queue in subscribers:
            subscribers.remove(queue)
        if not subscribers:
            self._subscribers.pop(channel, None)

    async def publish(self, channel: str, message: str) -> None:
        for queue in list(self._subscribers.get(channel, [])):
            await queue.put({"channel": channel, "data": message})


class _DummyPubSub:
    def __init__(self, broker: _MemoryBroker, *, fail_first: bool = False) -> None:
        self._broker = broker
        self._queue: asyncio.Queue[dict[str, str]] = asyncio.Queue()
        self._channels: set[str] = set()
        self._fail_first = fail_first
        self._failed = False

    async def subscribe(self, *channels: str) -> None:
        for channel in channels:
            if channel in self._channels:
                continue
            self._channels.add(channel)
            self._broker.register(channel, self._queue)

    async def unsubscribe(self, *channels: str) -> None:
        targets = channels or tuple(self._channels)
        for channel in targets:
            if channel in self._channels:
                self._channels.remove(channel)
                self._broker.unregister(channel, self._queue)

    async def get_message(self, ignore_subscribe_messages: bool = True, timeout: float | None = None) -> dict[str, Any] | None:
        if self._fail_first and not self._failed:
            self._failed = True
            raise ConnectionError("simulated disconnect")
        try:
            wait_timeout = timeout if timeout and timeout > 0 else 0.05
            item = await asyncio.wait_for(self._queue.get(), timeout=wait_timeout)
        except asyncio.TimeoutError:
            return None
        return {"type": "message", "channel": item["channel"], "data": item["data"]}

    async def close(self) -> None:
        await self.unsubscribe()


class _DummyRedis:
    def __init__(self, broker: _MemoryBroker, *, fail_first: bool = False) -> None:
        self._broker = broker
        self._fail_first = fail_first
        self._pubsub_created = False

    def pubsub(self) -> _DummyPubSub:
        should_fail = self._fail_first and not self._pubsub_created
        self._pubsub_created = True
        return _DummyPubSub(self._broker, fail_first=should_fail)

    async def publish(self, channel: str, message: str) -> None:
        await self._broker.publish(channel, message)


class _RedisFactory:
    def __init__(self) -> None:
        self.broker = _MemoryBroker()
        self.calls = 0
        self.last_redis: _DummyRedis | None = None

    async def __call__(self) -> _DummyRedis:
        redis = _DummyRedis(self.broker, fail_first=self.calls == 0)
        self.calls += 1
        self.last_redis = redis
        return redis


def test_pubsub_listener_recovers_from_disconnect(loop: asyncio.AbstractEventLoop) -> None:
    async def scenario() -> None:
        factory = _RedisFactory()
        listener = PubSubListener(redis_factory=factory.__call__, reconnect_delay=0.01)
        received: list[tuple[str, str]] = []
        event = asyncio.Event()

        async def callback(channel: str, message: str) -> None:
            received.append((channel, message))
            event.set()

        listener.register_callback("orders", callback)
        await listener.start()

        for _ in range(100):
            if factory.calls >= 2:
                break
            await asyncio.sleep(0.02)
        else:  # pragma: no cover - defensive guard for flaky timing
            pytest.fail("listener did not reconnect in time")

        assert factory.last_redis is not None
        await factory.last_redis.publish("orders", "payload-1")

        await asyncio.wait_for(event.wait(), timeout=2.0)
        assert received == [("orders", "payload-1")]

        await listener.stop()

    loop.run_until_complete(scenario())
