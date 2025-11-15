import asyncio

import pytest

from src.data_layer import DataSourceManager, DataSubscriptionRequest, DataSourceAdapter, DataSourceError, EventBus


class StubAdapter(DataSourceAdapter):
    channel = "market.tick"

    def __init__(self, event_bus: EventBus) -> None:
        super().__init__(event_bus)
        self.subscribe_calls = 0
        self.cancel_calls = 0
        self.snapshot_calls = 0
        self.history_calls = 0

    async def subscribe(self, request: DataSubscriptionRequest):  # type: ignore[override]
        self.subscribe_calls += 1

        async def _cancel() -> None:
            self.cancel_calls += 1

        return _cancel

    async def snapshot(self, request: DataSubscriptionRequest):  # type: ignore[override]
        self.snapshot_calls += 1
        return {"symbol": request.symbol, "channel": request.channel}

    async def history(self, request: DataSubscriptionRequest):  # type: ignore[override]
        self.history_calls += 1
        return [{"symbol": request.symbol, "interval": request.interval}]


@pytest.mark.asyncio
async def test_manager_registers_and_reuses_subscriptions():
    bus = EventBus()
    manager = DataSourceManager(bus)
    adapter = StubAdapter(bus)
    manager.register_source(adapter)

    request = DataSubscriptionRequest(channel="market.tick", symbol="ES")

    token1 = await manager.subscribe(request)
    token2 = await manager.subscribe(request)

    assert adapter.subscribe_calls == 1

    await manager.unsubscribe(token1)
    assert adapter.cancel_calls == 0

    await manager.unsubscribe(token2)
    assert adapter.cancel_calls == 1


@pytest.mark.asyncio
async def test_manager_snapshot_and_history():
    bus = EventBus()
    manager = DataSourceManager(bus)
    adapter = StubAdapter(bus)
    manager.register_source(adapter)

    request = DataSubscriptionRequest(channel="market.tick", symbol="MES", interval="1m")

    snapshot = await manager.snapshot(request)
    history = await manager.history(request)

    assert snapshot == {"symbol": "MES", "channel": "market.tick"}
    assert history == [{"symbol": "MES", "interval": "1m"}]


@pytest.mark.asyncio
async def test_manager_unknown_channel():
    manager = DataSourceManager()
    request = DataSubscriptionRequest(channel="market.tick")
    with pytest.raises(DataSourceError):
        await manager.subscribe(request)


@pytest.mark.asyncio
async def test_manager_ignores_force_restart_in_dedupe():
    bus = EventBus()
    manager = DataSourceManager(bus)
    adapter = StubAdapter(bus)
    manager.register_source(adapter)

    request1 = DataSubscriptionRequest(
        channel="market.tick", symbol="ES", options={"force_restart": False}
    )
    request2 = DataSubscriptionRequest(
        channel="market.tick", symbol="ES", options={"force_restart": True}
    )

    token1 = await manager.subscribe(request1)
    token2 = await manager.subscribe(request2)

    assert adapter.subscribe_calls == 1

    await manager.unsubscribe(token1)
    await manager.unsubscribe(token2)


