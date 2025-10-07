from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from src.data_layer import (
    DataSourceManager,
    DataSubscriptionRequest,
    EventBus,
    EventEnvelope,
    InMemoryCacheBackend,
    NoopPersistenceBackend,
)
from src.data_layer.adapters import MarketQuoteAdapter
from src.data_layer.quote_feed import InMemoryQuoteFeed


async def _wait_for(condition: Callable[[], bool], *, timeout: float = 1.0) -> None:
    """Poll *condition* until it returns ``True`` or the timeout expires."""

    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        if condition():
            return
        if asyncio.get_event_loop().time() >= deadline:
            raise AssertionError("condition was not satisfied within the allotted time")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_market_quote_adapter_emits_ticks_and_candles() -> None:
    bus = EventBus()
    manager = DataSourceManager(bus)
    feed = InMemoryQuoteFeed()
    cache = InMemoryCacheBackend()
    persistence = NoopPersistenceBackend()

    adapter = MarketQuoteAdapter(feed, bus, cache, persistence, intervals=("1s",))
    manager.register_source(adapter)

    tick_events: list[EventEnvelope] = []
    candle_events: list[EventEnvelope] = []

    def handler(envelope: EventEnvelope) -> None:
        if envelope.topic == "market.tick":
            tick_events.append(envelope)
        elif envelope.topic.startswith("market.candle"):
            candle_events.append(envelope)

    bus.subscribe("market.*", handler)

    request = DataSubscriptionRequest(channel="market.tick", symbol="ES")
    token = await manager.subscribe(request)

    await feed.publish_tick("ES", {"last": 4400.0, "volume": 1.0, "ts_ns": 1_000_000_000})
    await feed.publish_tick("ES", {"last": 4401.0, "volume": 2.0, "ts_ns": 2_500_000_000})

    await _wait_for(lambda: len(tick_events) >= 2)
    await _wait_for(lambda: any(event.payload.get("is_closed") for event in candle_events))

    snapshot = await manager.snapshot(DataSubscriptionRequest(channel="market.tick", symbol="ES"))
    assert snapshot["last"] == pytest.approx(4401.0)

    history = await manager.history(
        DataSubscriptionRequest(channel="market.tick", symbol="ES", interval="1s")
    )
    assert history, "expected candle history"
    assert history[-1]["close"] == pytest.approx(4401.0)

    await manager.unsubscribe(token)

    await feed.publish_tick("ES", {"last": 4402.0, "volume": 3.0, "ts_ns": 3_500_000_000})
    await asyncio.sleep(0.05)
    assert len(tick_events) == 2, "unsubscribe should stop additional tick events"

