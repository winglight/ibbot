from __future__ import annotations

import pytest

from src.data_layer import (
    DataSourceAdapter,
    DataSourceError,
    DataSourceManager,
    DataSubscriptionRequest,
    EventBus,
)


class RecordingAdapter(DataSourceAdapter):
    """In-memory adapter to exercise :class:`DataSourceManager`."""

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
async def test_manager_reuses_active_subscription_tokens() -> None:
    bus = EventBus()
    manager = DataSourceManager(bus)
    adapter = RecordingAdapter(bus)
    manager.register_source(adapter)

    request = DataSubscriptionRequest(channel="market.tick", symbol="ES")

    token_1 = await manager.subscribe(request)
    token_2 = await manager.subscribe(request)

    assert adapter.subscribe_calls == 1, "identical requests should reuse the same adapter stream"

    await manager.unsubscribe(token_1)
    assert adapter.cancel_calls == 0, "first unsubscribe should only decrement the refcount"

    await manager.unsubscribe(token_2)
    assert adapter.cancel_calls == 1, "adapter cancel should run when the last subscriber leaves"


@pytest.mark.asyncio
async def test_manager_snapshot_and_history_passthrough() -> None:
    bus = EventBus()
    manager = DataSourceManager(bus)
    adapter = RecordingAdapter(bus)
    manager.register_source(adapter)

    request = DataSubscriptionRequest(channel="market.tick", symbol="MES", interval="1m")

    snapshot = await manager.snapshot(request)
    history = await manager.history(request)

    assert snapshot == {"symbol": "MES", "channel": "market.tick"}
    assert history == [{"symbol": "MES", "interval": "1m"}]
    assert adapter.snapshot_calls == 1
    assert adapter.history_calls == 1


@pytest.mark.asyncio
async def test_manager_errors_for_unknown_channels() -> None:
    manager = DataSourceManager()
    request = DataSubscriptionRequest(channel="market.unknown")

    with pytest.raises(DataSourceError):
        await manager.subscribe(request)


@pytest.mark.asyncio
async def test_force_restart_does_not_break_deduplication() -> None:
    bus = EventBus()
    manager = DataSourceManager(bus)
    adapter = RecordingAdapter(bus)
    manager.register_source(adapter)

    request = DataSubscriptionRequest(
        channel="market.tick",
        symbol="ES",
        options={"force_restart": False},
    )
    restart_request = DataSubscriptionRequest(
        channel="market.tick",
        symbol="ES",
        options={"force_restart": True},
    )

    token_1 = await manager.subscribe(request)
    token_2 = await manager.subscribe(restart_request)

    assert adapter.subscribe_calls == 1, "force_restart should be ignored for dedupe comparisons"

    await manager.unsubscribe(token_1)
    await manager.unsubscribe(token_2)

    assert adapter.cancel_calls == 1
