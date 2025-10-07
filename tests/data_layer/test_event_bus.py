from __future__ import annotations

import asyncio

import pytest

from src.data_layer.event_bus import EventBus, EventEnvelope


@pytest.mark.asyncio
async def test_subscribe_invokes_sync_handler() -> None:
    bus = EventBus()
    observed: list[str] = []

    def handler(event: EventEnvelope) -> None:
        observed.append(event.topic)

    bus.subscribe("market.*", handler)

    await bus.publish(EventEnvelope(event_type="market.tick", topic="market.price"))

    assert observed == ["market.price"]


@pytest.mark.asyncio
async def test_subscribe_invokes_async_handler() -> None:
    bus = EventBus()
    called = asyncio.Event()

    async def handler(event: EventEnvelope) -> None:
        if event.topic == "dom.metrics":
            called.set()

    bus.subscribe("dom.*", handler)

    await bus.publish(EventEnvelope(event_type="dom.metrics", topic="dom.metrics"))

    assert called.is_set()


@pytest.mark.asyncio
async def test_unsubscribe_prevents_additional_delivery() -> None:
    bus = EventBus()
    observed: list[str] = []

    def handler(event: EventEnvelope) -> None:
        observed.append(event.topic)

    token = bus.subscribe("strategy.*", handler)
    bus.unsubscribe(token)

    await bus.publish(EventEnvelope(event_type="strategy.event", topic="strategy.status"))

    assert observed == []


def test_publish_nowait_requires_running_loop() -> None:
    bus = EventBus()

    with pytest.raises(RuntimeError):
        bus.publish_nowait(EventEnvelope(event_type="noop", topic="noop"))


@pytest.mark.asyncio
async def test_publish_nowait_schedules_delivery() -> None:
    bus = EventBus()
    observed: list[str] = []
    event = asyncio.Event()

    async def handler(envelope: EventEnvelope) -> None:
        observed.append(envelope.topic)
        event.set()

    bus.subscribe("market.*", handler)

    bus.publish_nowait(EventEnvelope(event_type="market.tick", topic="market.depth"))

    await asyncio.wait_for(event.wait(), timeout=1)

    assert observed == ["market.depth"]

