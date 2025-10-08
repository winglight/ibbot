import asyncio
from collections import deque
from typing import Any, Mapping, Sequence

import pytest

try:  # pragma: no cover - executed when real ibapi is available
    from ibapi.contract import Contract
except ImportError:  # pragma: no cover - fallback for test environments
    from test.ibapi_stub import Contract  # type: ignore

from src.ibapi_controller import IBAPIController
from src.market_data_router import MarketDataRouter


@pytest.mark.asyncio
async def test_router_emits_price_events() -> None:
    loop = asyncio.get_running_loop()
    controller = IBAPIController(loop=loop)

    await controller.connect()
    await controller.wait_until_connected()
    await asyncio.wait_for(controller.event_queue.get(), timeout=1)

    price_events: deque[tuple[str, Mapping[str, Any]]] = deque()
    depth_events: deque[tuple[str, Sequence[Mapping[str, Any]]]] = deque()
    disconnect_calls: deque[bool] = deque()

    async def _get_controller() -> IBAPIController:
        return controller

    router = MarketDataRouter(
        controller_getter=_get_controller,
        loop=loop,
        price_listener=lambda symbol, payload: price_events.append((symbol, payload)),
        depth_listener=lambda symbol, levels: depth_events.append((symbol, tuple(levels))),
        disconnect_listener=lambda: disconnect_calls.append(True),
    )

    contract = Contract()
    contract.symbol = "AAPL"
    await router.subscribe(symbol="AAPL", contract=contract, dom_rows=0)

    client = controller.client
    price_req_id = next(iter(client._market_data_requests.keys()))
    client.emit_tick_price(price_req_id, 1, 100.5)
    client.emit_tick_size(price_req_id, 0, 12)
    client.emit_market_data_type(price_req_id, 1)

    for _ in range(10):
        if price_events:
            break
        await asyncio.sleep(0.01)

    assert price_events, "Expected a price event to be emitted"
    symbol, payload = price_events[-1]
    assert symbol == "AAPL"
    assert payload["bid"] == 100.5
    assert payload["bid_size"] == 12.0
    assert payload["market_state"] == "real-time"

    await router.unsubscribe("AAPL")
    await router.shutdown()
    await controller.disconnect()


@pytest.mark.asyncio
async def test_router_emits_depth_events() -> None:
    loop = asyncio.get_running_loop()
    controller = IBAPIController(loop=loop)

    await controller.connect()
    await controller.wait_until_connected()
    await asyncio.wait_for(controller.event_queue.get(), timeout=1)

    depth_events: deque[tuple[str, Sequence[Mapping[str, Any]]]] = deque()

    async def _get_controller() -> IBAPIController:
        return controller

    router = MarketDataRouter(
        controller_getter=_get_controller,
        loop=loop,
        price_listener=lambda *_args: None,
        depth_listener=lambda symbol, levels: depth_events.append((symbol, tuple(levels))),
        disconnect_listener=lambda: None,
    )

    contract = Contract()
    contract.symbol = "ES"
    await router.subscribe(symbol="ES", contract=contract, dom_rows=5)

    client = controller.client
    depth_req_id = next(iter(client._market_depth_requests.keys()))
    client.emit_depth_update(depth_req_id, 0, "MM", 0, 1, 100.0, 2.0)
    client.emit_depth_update(depth_req_id, 0, "MM", 0, 0, 100.25, 1.5)

    for _ in range(10):
        if depth_events:
            break
        await asyncio.sleep(0.01)

    assert depth_events, "Expected depth events"
    symbol, levels = depth_events[-1]
    assert symbol == "ES"
    assert any(level["side"] == "bid" for level in levels)
    assert any(level["side"] == "ask" for level in levels)

    await router.unsubscribe("ES")
    await router.shutdown()
    await controller.disconnect()


@pytest.mark.asyncio
async def test_router_notifies_disconnect_on_connection_close() -> None:
    loop = asyncio.get_running_loop()
    controller = IBAPIController(loop=loop)

    await controller.connect()
    await controller.wait_until_connected()
    await asyncio.wait_for(controller.event_queue.get(), timeout=1)

    disconnect_calls: deque[bool] = deque()

    async def _get_controller() -> IBAPIController:
        return controller

    router = MarketDataRouter(
        controller_getter=_get_controller,
        loop=loop,
        price_listener=lambda *_args: None,
        depth_listener=lambda *_args: None,
        disconnect_listener=lambda: disconnect_calls.append(True),
    )

    contract = Contract()
    contract.symbol = "CL"
    await router.subscribe(symbol="CL", contract=contract, dom_rows=0)

    controller.connectionClosed()

    for _ in range(10):
        if disconnect_calls:
            break
        await asyncio.sleep(0.01)

    assert disconnect_calls, "Expected disconnect callback"

    await router.unsubscribe("CL")
    await router.shutdown()
    await controller.disconnect()
