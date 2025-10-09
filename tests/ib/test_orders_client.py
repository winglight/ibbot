from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("ibapi.execution")

from services.orders.ib_client import ServiceIBClient


@pytest.fixture
async def prepared_client() -> ServiceIBClient:
    client = ServiceIBClient()
    loop = asyncio.get_running_loop()
    client._loop = loop  # type: ignore[attr-defined]
    client._connection_ready = asyncio.Event()  # type: ignore[attr-defined]
    client._connection_ready.set()  # type: ignore[attr-defined]
    client._callback_queue = asyncio.Queue()  # type: ignore[attr-defined]
    client._async_request_lock = asyncio.Lock()  # type: ignore[attr-defined]
    client.isConnected = lambda: True  # type: ignore[assignment]
    return client


def _build_contract(**overrides: Any) -> SimpleNamespace:
    defaults = {
        "conId": 1,
        "symbol": "AAPL",
        "secType": "STK",
        "exchange": "SMART",
        "currency": "USD",
        "localSymbol": "AAPL",
        "tradingClass": "NMS",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _build_order(**overrides: Any) -> SimpleNamespace:
    defaults = {
        "action": "BUY",
        "orderType": "MKT",
        "totalQuantity": 100,
        "tif": "DAY",
        "outsideRth": False,
        "account": "DU123",
        "permId": 123,
        "transmit": True,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _build_order_state(**overrides: Any) -> SimpleNamespace:
    defaults = {
        "status": "Submitted",
        "completedTime": "20240101 12:00:00",
        "warningText": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.mark.asyncio
async def test_place_order_waits_for_status(prepared_client: ServiceIBClient) -> None:
    client = prepared_client
    calls: list[tuple[int, Any, Any]] = []

    async def _next_request_id() -> int:
        return 42

    client.next_request_id = _next_request_id  # type: ignore[assignment]
    client.placeOrder = lambda order_id, contract, order: calls.append((order_id, contract, order))  # type: ignore[assignment]

    async def _emit() -> None:
        await asyncio.sleep(0)
        client.orderStatus(42, "Submitted", 0.0, 100.0, 0.0, 1, 0, 0.0, 0, "", 0.0)

    asyncio.create_task(_emit())

    result = await client.place_order(_build_contract(), _build_order())
    assert calls and calls[0][0] == 42
    assert result.status == "Submitted"

    event_type, payload = await client.callback_queue.get()  # type: ignore[attr-defined]
    assert event_type == "order_status"
    assert payload.order_id == 42


@pytest.mark.asyncio
async def test_cancel_order_waits_for_cancelled(prepared_client: ServiceIBClient) -> None:
    client = prepared_client
    cancelled: list[int] = []

    client.cancelOrder = lambda order_id: cancelled.append(order_id)  # type: ignore[assignment]

    async def _emit() -> None:
        await asyncio.sleep(0)
        client.orderStatus(7, "Cancelled", 0.0, 0.0, 0.0, 1, 0, 0.0, 0, "", 0.0)

    asyncio.create_task(_emit())

    result = await client.cancel_order(7)
    assert cancelled == [7]
    assert result.status == "Cancelled"


@pytest.mark.asyncio
async def test_fetch_open_orders_collects_results(prepared_client: ServiceIBClient) -> None:
    client = prepared_client
    requested: list[str] = []

    client.reqOpenOrders = lambda: requested.append("open")  # type: ignore[assignment]

    async def _emit() -> None:
        await asyncio.sleep(0)
        client.openOrder(1, _build_contract(), _build_order(), _build_order_state())
        client.openOrderEnd()

    asyncio.create_task(_emit())

    orders = await client.fetch_open_orders()
    assert requested == ["open"]
    assert len(orders) == 1
    assert orders[0]["order_id"] == 1


@pytest.mark.asyncio
async def test_fetch_completed_orders_filters_by_days(prepared_client: ServiceIBClient) -> None:
    client = prepared_client
    client.reqCompletedOrders = lambda api_only: None  # type: ignore[assignment]

    async def _emit() -> None:
        await asyncio.sleep(0)
        client.completedOrder(
            _build_contract(symbol="OLD"),
            _build_order(),
            _build_order_state(completedTime="20230101 12:00:00"),
        )
        client.completedOrder(
            _build_contract(symbol="NEW"),
            _build_order(),
            _build_order_state(completedTime="20240102 00:00:00"),
        )
        client.completedOrdersEnd()

    asyncio.create_task(_emit())

    results = await client.fetch_completed_orders(days_back=60)
    symbols = {entry["contract"]["symbol"] for entry in results}
    assert symbols == {"NEW"}


@pytest.mark.asyncio
async def test_fetch_executions_collects_commission(prepared_client: ServiceIBClient) -> None:
    client = prepared_client
    requests: list[int] = []

    async def _next_request_id() -> int:
        return 99

    client.next_request_id = _next_request_id  # type: ignore[assignment]
    client.reqExecutions = lambda req_id, filt: requests.append(req_id)  # type: ignore[assignment]

    execution = SimpleNamespace(
        execId="abc",
        orderId=10,
        clientId=1,
        permId=2,
        symbol="AAPL",
        secType="STK",
        exchange="SMART",
        side="BOT",
        shares=5.0,
        price=100.0,
        cumQty=5.0,
        avgPrice=100.0,
    )
    commission_report = SimpleNamespace(execId="abc", commission=1.25, currency="USD")

    async def _emit() -> None:
        await asyncio.sleep(0)
        client.execDetails(99, _build_contract(), execution)
        client.commissionReport(commission_report)
        client.execDetailsEnd(99)

    asyncio.create_task(_emit())

    results = await client.fetch_executions(filters={"symbol": "AAPL"})
    assert requests == [99]
    assert len(results) == 1
    assert results[0]["execution"]["exec_id"] == "abc"
    assert results[0]["commission"]["commission"] == 1.25


@pytest.mark.asyncio
async def test_refresh_order_ids_requests_from_server(prepared_client: ServiceIBClient) -> None:
    client = prepared_client
    requests: list[int] = []

    client.reqIds = lambda count: requests.append(count)  # type: ignore[assignment]

    await client.refresh_order_ids()
    assert requests == [1]
