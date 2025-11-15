import asyncio
from datetime import datetime

import pytest

from src.data_layer import DataSourceManager, DataSubscriptionRequest, EventEnvelope
from src.data_layer.event_bus import EventBus
from src.data_layer.adapters import DomDepthAdapter


class StubDepthSubscriber:
    def __init__(self) -> None:
        self.callbacks = {}
        self.active = set()
        self.last_force_restart = False

    def start_depth_stream(self, symbol, callback, num_rows=5, force_restart=False):
        self.callbacks[symbol] = callback
        self.active.add(symbol)
        self.last_force_restart = force_restart
        return True

    def stop_depth_stream(self, symbol):
        self.active.discard(symbol)
        self.callbacks.pop(symbol, None)
        return True

    def get_last_error(self):
        return None, 500

    def emit(self, symbol, bids, asks):
        callback = self.callbacks[symbol]
        payload = {
            "bids": bids,
            "asks": asks,
            "timestamp": datetime.utcnow(),
        }
        callback(payload)


@pytest.mark.asyncio
async def test_dom_depth_adapter_publishes_events():
    subscriber = StubDepthSubscriber()
    bus = EventBus()
    manager = DataSourceManager(bus)
    adapter = DomDepthAdapter(subscriber, bus, max_history=3)
    manager.register_source(adapter)

    events: list[EventEnvelope] = []

    async def handler(envelope: EventEnvelope) -> None:
        events.append(envelope)

    bus.subscribe("dom.depth", handler)

    request = DataSubscriptionRequest(channel="dom.depth", symbol="ES")
    token = await manager.subscribe(request)

    subscriber.emit("ES", [(4400.0, 5)], [(4400.5, 3)])
    await asyncio.sleep(0)

    snapshot = await manager.snapshot(request)
    history = await manager.history(request)

    assert subscriber.active == {"ES"}
    assert len(events) == 1
    assert snapshot["symbol"] == "ES"
    assert history and history[0]["symbol"] == "ES"

    await manager.unsubscribe(token)

    assert "ES" not in subscriber.active
