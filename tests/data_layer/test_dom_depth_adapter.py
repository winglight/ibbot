from __future__ import annotations

import asyncio

from collections.abc import Callable
from datetime import datetime, timezone

import pytest

from src.data_layer import (
    DataSourceManager,
    DataSubscriptionRequest,
    EventBus,
    EventEnvelope,
)
from src.data_layer.adapters import DomDepthAdapter


class RecordingDepthSubscriber:
    """In-memory stand in for exercising :class:`DomDepthAdapter`."""

    def __init__(self) -> None:
        self._callbacks: dict[str, Callable[[dict[str, object]], None]] = {}
        self.start_calls: list[tuple[str, int, bool]] = []
        self.stop_calls: list[str] = []
        self.last_error: tuple[str | None, int] = (None, 500)

    def start_depth_stream(
        self,
        symbol: str,
        callback: Callable[[dict[str, object]], None],
        *,
        num_rows: int = 5,
        force_restart: bool = False,
    ) -> bool:
        symbol_key = symbol.upper()
        self.start_calls.append((symbol_key, int(num_rows), bool(force_restart)))
        self._callbacks[symbol_key] = callback
        return True

    def stop_depth_stream(self, symbol: str) -> bool:
        symbol_key = symbol.upper()
        self.stop_calls.append(symbol_key)
        self._callbacks.pop(symbol_key, None)
        return True

    def get_last_error(self) -> tuple[str | None, int]:
        return self.last_error

    def emit(
        self,
        symbol: str,
        *,
        bids: list[tuple[float, float]] | None = None,
        asks: list[tuple[float, float]] | None = None,
        ts_ns: int | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        payload = {
            "bids": list(bids or ()),
            "asks": list(asks or ()),
            "ts_ns": ts_ns,
            "timestamp": timestamp or datetime.now(timezone.utc),
        }
        callback = self._callbacks[symbol.upper()]
        callback(payload)


async def _wait_for(condition: Callable[[], bool], *, timeout: float = 1.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        if condition():
            return
        if asyncio.get_event_loop().time() >= deadline:
            raise AssertionError("condition was not satisfied within the allotted time")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_dom_depth_adapter_publishes_events_and_caches_history() -> None:
    bus = EventBus()
    manager = DataSourceManager(bus)
    subscriber = RecordingDepthSubscriber()
    adapter = DomDepthAdapter(subscriber, bus, max_history=2)
    manager.register_source(adapter)

    received: list[EventEnvelope] = []

    async def handler(envelope: EventEnvelope) -> None:
        received.append(envelope)

    bus.subscribe("dom.depth", handler)

    request = DataSubscriptionRequest(
        channel="dom.depth",
        symbol="ES",
        options={"num_rows": 3},
    )
    token = await manager.subscribe(request)

    assert subscriber.start_calls == [("ES", 3, False)]

    subscriber.emit(
        "ES",
        bids=[(4400.0, 3.0)],
        asks=[(4400.5, 2.0)],
        ts_ns=123,
        timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )

    await _wait_for(lambda: len(received) == 1)

    event = received[0]
    assert event.topic == "dom.depth"
    assert event.payload["bids"] == [(4400.0, 3.0)]
    assert event.payload["asks"] == [(4400.5, 2.0)]
    assert event.meta == {"num_rows": 3}

    snapshot = await manager.snapshot(request)
    assert snapshot["symbol"] == "ES"
    assert snapshot["bids"] == [(4400.0, 3.0)]
    assert snapshot["asks"] == [(4400.5, 2.0)]
    assert snapshot["ts_ns"] == 123
    assert snapshot["timestamp"]

    history = await manager.history(request)
    assert [entry["symbol"] for entry in history] == ["ES"]

    subscriber.emit("ES", bids=[(4401.0, 1.0)], asks=[(4401.5, 1.0)])
    await _wait_for(lambda: len(received) == 2)

    history = await manager.history(request)
    assert len(history) == 2

    await manager.unsubscribe(token)
    assert subscriber.stop_calls == ["ES"]


@pytest.mark.asyncio
async def test_dom_depth_adapter_honours_force_restart() -> None:
    bus = EventBus()
    manager = DataSourceManager(bus)
    subscriber = RecordingDepthSubscriber()
    adapter = DomDepthAdapter(subscriber, bus)
    manager.register_source(adapter)

    request = DataSubscriptionRequest(channel="dom.depth", symbol="NQ")
    token = await manager.subscribe(request)
    await manager.unsubscribe(token)

    subscriber.stop_calls.clear()
    subscriber.start_calls.clear()

    request_restart = DataSubscriptionRequest(
        channel="dom.depth",
        symbol="NQ",
        options={"force_restart": True},
    )

    token = await manager.subscribe(request_restart)
    assert subscriber.stop_calls == ["NQ"], "force_restart should stop the existing stream first"
    assert subscriber.start_calls == [("NQ", 5, True)]

    await manager.unsubscribe(token)
