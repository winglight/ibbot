import asyncio

import pytest

from src.data_layer import (
    DataSourceManager,
    DataSubscriptionRequest,
    EventEnvelope,
    EventBus,
    InMemoryCacheBackend,
    NoopPersistenceBackend,
)
from src.data_layer.adapters import MarketQuoteAdapter
from src.data_layer.quote_feed import InMemoryQuoteFeed


@pytest.mark.asyncio
async def test_market_quote_adapter_emits_ticks_and_candles():
    bus = EventBus()
    manager = DataSourceManager(bus)
    feed = InMemoryQuoteFeed()
    cache = InMemoryCacheBackend()
    persistence = NoopPersistenceBackend()

    adapter = MarketQuoteAdapter(feed, bus, cache, persistence, intervals=("1s",))
    manager.register_source(adapter)

    tick_events: list[EventEnvelope] = []
    candle_events: list[EventEnvelope] = []

    async def handler(envelope: EventEnvelope) -> None:
        if envelope.topic == "market.tick":
            tick_events.append(envelope)
        elif envelope.topic.startswith("market.candle"):
            candle_events.append(envelope)

    bus.subscribe("market.*", handler)

    request = DataSubscriptionRequest(channel="market.tick", symbol="ES")
    token = await manager.subscribe(request)

    await feed.publish_tick("ES", {"last": 4400.0, "volume": 1.0, "ts_ns": 1_000_000_000})
    await feed.publish_tick("ES", {"last": 4401.0, "volume": 2.0, "ts_ns": 2_500_000_000})
    await asyncio.sleep(0.01)

    assert len(tick_events) == 2
    assert candle_events, "expected candle events"
    assert any(event.payload.get("is_closed") for event in candle_events)

    snapshot = await manager.snapshot(DataSubscriptionRequest(channel="market.tick", symbol="ES"))
    assert snapshot["last"] == pytest.approx(4401.0)

    history = await manager.history(
        DataSubscriptionRequest(channel="market.tick", symbol="ES", interval="1s")
    )
    assert history, "expected candle history"
    assert history[-1]["close"] == pytest.approx(4401.0)

    await manager.unsubscribe(token)

