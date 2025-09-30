"""Tests for the Redis-backed configuration provider."""

from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from typing import Any, Callable, Dict

import pytest

from src.config import load_project_env
from src.ibbot.config import ConfigProvider


class FakePubSub:
    """A minimal Pub/Sub implementation backed by asyncio queues."""

    def __init__(self, parent: "FakeRedis") -> None:
        self._parent = parent
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._subscribed: set[str] = set()
        self._closed = False

    async def subscribe(self, *channels: str) -> None:
        for channel in channels:
            if channel not in self._subscribed:
                self._subscribed.add(channel)
                self._parent._subscribe(channel, self)

    async def unsubscribe(self, *channels: str) -> None:
        targets = channels or tuple(self._subscribed)
        for channel in targets:
            if channel in self._subscribed:
                self._subscribed.remove(channel)
                self._parent._unsubscribe(channel, self)

    async def close(self) -> None:
        self._closed = True
        await self._queue.put(None)

    async def listen(self):
        while True:
            message = await self._queue.get()
            if message is None:
                break
            yield message

    async def push(self, channel: str, data: Any) -> None:
        await self._queue.put({"type": "message", "channel": channel, "data": data})


class FakeRedis:
    """Tiny subset of the Redis API used by :class:`ConfigProvider`."""

    def __init__(self) -> None:
        self._hashes: Dict[str, Dict[str, str]] = defaultdict(dict)
        self._subscribers: Dict[str, set[FakePubSub]] = defaultdict(set)

    async def hgetall(self, key: str) -> Dict[str, str]:
        return dict(self._hashes.get(key, {}))

    async def hset(self, key: str, mapping: Dict[str, Any]) -> None:
        bucket = self._hashes[key]
        for field, value in mapping.items():
            bucket[str(field)] = str(value)

    def pubsub(self) -> FakePubSub:
        return FakePubSub(self)

    async def publish(self, channel: str, data: Any) -> None:
        for subscriber in tuple(self._subscribers.get(channel, set())):
            await subscriber.push(channel, data)

    def _subscribe(self, channel: str, pubsub: FakePubSub) -> None:
        self._subscribers[channel].add(pubsub)

    def _unsubscribe(self, channel: str, pubsub: FakePubSub) -> None:
        bucket = self._subscribers.get(channel)
        if not bucket:
            return
        bucket.discard(pubsub)
        if not bucket:
            self._subscribers.pop(channel, None)


async def wait_until(condition: Callable[[], bool], timeout: float = 1.0) -> None:
    """Utility helper to wait for a condition to become truthy."""

    end_time = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end_time:
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


def test_config_provider_merges_env_and_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        load_project_env.cache_clear()
        monkeypatch.delenv("STOP_LOSS_RATIO", raising=False)

        fake = FakeRedis()
        provider = ConfigProvider(redis_client=fake)

        merged = await provider.get_module("risk")
        assert merged["STOP_LOSS_RATIO"] == os.getenv("STOP_LOSS_RATIO")

        await fake.hset("hash:config:risk", {"STOP_LOSS_RATIO": "0.07", "EXTRA": "abc"})
        await provider.refresh("risk")

        assert await provider.get("risk", "STOP_LOSS_RATIO", type_=float) == pytest.approx(0.07)
        assert await provider.get("risk", "EXTRA") == "abc"
        assert await provider.get("risk", "UNKNOWN", default="fallback") == "fallback"

    asyncio.run(scenario())


def test_config_provider_handles_pubsub_updates(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        load_project_env.cache_clear()
        monkeypatch.delenv("STOP_LOSS_RATIO", raising=False)

        fake = FakeRedis()
        provider = ConfigProvider(redis_client=fake)

        updates: list[tuple[str, str]] = []

        async def listener(module: str, payload: Dict[str, str]) -> None:
            updates.append((module, payload["STOP_LOSS_RATIO"]))

        provider.subscribe("risk", listener)

        await provider.start()
        try:
            await fake.hset("hash:config:risk", {"STOP_LOSS_RATIO": "0.12"})
            await fake.publish("pubsub:config:update", "risk")
            await wait_until(lambda: bool(updates))
        finally:
            await provider.close()

        assert updates == [("risk", "0.12")]
        assert await provider.get("risk", "STOP_LOSS_RATIO", type_=float) == pytest.approx(0.12)

    asyncio.run(scenario())
