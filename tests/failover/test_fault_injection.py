"""Fault injection tests ensuring dependency failures trigger recovery paths."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field

import pytest

from src.ibbot.influx import (
    InfluxPoint,
    configure_influx,
    get_write_statistics,
    reset_write_statistics,
    write_points,
)
from src.ibbot.redis import PubSubListener
from services.ib_feed.main import MarketDataFeedService


class _LogCapture(logging.Handler):
    def __init__(self, level: int) -> None:
        super().__init__(level)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - trivial
        self.records.append(record)


@contextmanager
def _capture_logs(logger_name: str, level: int) -> Iterator[list[logging.LogRecord]]:
    logger = logging.getLogger(logger_name)
    handler = _LogCapture(level)
    logger.addHandler(handler)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)


class _FlakyInfluxClient:
    """A minimal Influx client that simulates a transient outage."""

    def __init__(self) -> None:
        self.calls = 0
        self.fail_next = True

    def write_api(self, *, write_options: object | None = None) -> "_FlakyInfluxClient":
        return self

    def write(
        self,
        *,
        bucket: str,
        org: str,
        record: Iterable[Mapping[str, object]],
        write_precision: str,
    ) -> None:
        self.calls += 1
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated influx outage")


class _FakePubSub:
    def __init__(self, *, disconnect_once: asyncio.Event | None = None) -> None:
        self._disconnect_once = disconnect_once
        self._closed = False

    async def subscribe(self, *channels: str) -> None:  # pragma: no cover - trivial
        return None

    async def get_message(
        self,
        *,
        ignore_subscribe_messages: bool,
        timeout: float,
    ) -> Mapping[str, str] | None:
        await asyncio.sleep(0)
        if self._disconnect_once is not None and not self._disconnect_once.is_set():
            self._disconnect_once.set()
            raise ConnectionError("simulated redis disconnect")
        return None

    async def close(self) -> None:
        self._closed = True


class _FakeRedis:
    def __init__(self, *, disconnect_once: asyncio.Event | None = None) -> None:
        self._disconnect_once = disconnect_once

    def pubsub(self) -> _FakePubSub:
        return _FakePubSub(disconnect_once=self._disconnect_once)


class _RecordingRedis:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []
        self.hashes: dict[str, dict[str, str]] = {}

    async def publish(self, channel: str, payload: str) -> None:
        self.published.append((channel, payload))

    async def hset(self, key: str, *, mapping: Mapping[str, str]) -> None:
        bucket = self.hashes.setdefault(key, {})
        bucket.update(mapping)


@dataclass
class _FakeIBClient:
    subscriptions: list[str] = field(default_factory=list)
    connect_calls: int = 0
    reconnect_event: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        self._disconnect_listeners: list[callable] = []

    async def connect(self) -> None:
        self.connect_calls += 1
        self.reconnect_event.set()

    async def subscribe_market_data(self, symbol: str) -> None:
        self.subscriptions.append(symbol)

    def add_price_listener(self, callback: callable) -> None:  # pragma: no cover - unused
        return None

    def add_dom_listener(self, callback: callable) -> None:  # pragma: no cover - unused
        return None

    def add_disconnect_listener(self, callback: callable) -> None:
        self._disconnect_listeners.append(callback)

    def trigger_disconnect(self) -> None:
        self.reconnect_event.clear()
        for callback in list(self._disconnect_listeners):
            callback()

    async def disconnect(self) -> None:  # pragma: no cover - unused
        return None


async def _wait_for(predicate: callable, *, timeout: float = 1.0) -> None:
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


def test_influx_write_failure_updates_statistics() -> None:
    configure_influx(url="http://localhost", token="token", org="org", bucket="bucket")
    reset_write_statistics()
    client = _FlakyInfluxClient()

    with pytest.raises(RuntimeError, match="simulated influx outage"):
        write_points([InfluxPoint("m", fields={"value": 1})], client=client)

    stats = get_write_statistics()
    assert stats.batches == 1
    assert stats.points == 1
    assert stats.last_error == "simulated influx outage"

    result = write_points([InfluxPoint("m", fields={"value": 2})], client=client)
    assert result == 1

    stats = get_write_statistics()
    assert stats.batches == 2
    assert stats.points == 2
    assert stats.last_error is None
    assert client.calls == 2


def test_redis_pubsub_listener_recovers_from_disconnect() -> None:
    async def _run() -> list[logging.LogRecord]:
        disconnect_flag = asyncio.Event()

        async def fake_factory() -> _FakeRedis:
            if not disconnect_flag.is_set():
                return _FakeRedis(disconnect_once=disconnect_flag)
            return _FakeRedis()

        listener = PubSubListener(redis_factory=fake_factory, reconnect_delay=0.01)
        listener.register_callback("channel", lambda *_: None)

        with _capture_logs("src.ibbot.redis", logging.WARNING) as records:
            await listener.start()
            await asyncio.sleep(0.05)
            await listener.stop()
        return records

    records = asyncio.run(_run())
    assert any("Redis pubsub disconnected" in record.message for record in records)


def test_ib_feed_reconnects_after_disconnect() -> None:
    async def _run() -> list[logging.LogRecord]:
        ib_client = _FakeIBClient()
        redis = _RecordingRedis()

        async def fake_write(points: Iterable[Mapping[str, object]]) -> int:
            return len(list(points))

        service = MarketDataFeedService(
            ib_client=ib_client,
            redis=redis,
            subscriptions=["AAPL"],
            write_fn=fake_write,
            reconnect_delay=0.01,
        )

        with _capture_logs("ibbot.services.ib_feed", logging.INFO) as records:
            await service.start()
            assert ib_client.connect_calls == 1
            assert ib_client.subscriptions == ["AAPL"]

            ib_client.trigger_disconnect()
            await _wait_for(lambda: ib_client.connect_calls >= 2)

            await service.stop()
        return records

    records = asyncio.run(_run())
    messages = " ".join(record.message for record in records)
    assert "IB client disconnected; scheduling reconnect" in messages
    assert "Reconnected to IB and restored subscriptions" in messages
