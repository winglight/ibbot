import asyncio

import pytest

from src.data_layer.event_bus import EventBus, EventEnvelope


@pytest.mark.asyncio
async def test_event_bus_invokes_sync_handler():
    bus = EventBus()
    observed: list[str] = []

    def handler(event: EventEnvelope) -> None:
        observed.append(event.topic)

    bus.subscribe("market.*", handler)

    await bus.publish(EventEnvelope(event_type="market.tick", topic="market.price"))

    assert observed == ["market.price"]


@pytest.mark.asyncio
async def test_event_bus_invokes_async_handler():
    bus = EventBus()
    called = asyncio.Event()

    async def handler(event: EventEnvelope) -> None:
        if event.topic == "dom.metrics":
            called.set()

    bus.subscribe("dom.*", handler)

    await bus.publish(EventEnvelope(event_type="dom.metrics", topic="dom.metrics"))

    assert called.is_set()


@pytest.mark.asyncio
async def test_event_bus_unsubscribe_stops_delivery():
    bus = EventBus()
    observed: list[str] = []

    def handler(event: EventEnvelope) -> None:
        observed.append(event.topic)

    token = bus.subscribe("strategy.*", handler)
    bus.unsubscribe(token)

    await bus.publish(EventEnvelope(event_type="strategy.event", topic="strategy.status"))

    assert not observed


