import asyncio
import threading
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.orders.main import app, _state
from src.ib.events import ExecutionEvent, OrderStatusEvent


class _StubPublisher:
    def __init__(self) -> None:
        self.messages: list[tuple[str, Any]] = []
        self.signal = threading.Event()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _StubOrdersClient:
    def __init__(self) -> None:
        self.callback_queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.connected = False
        self.place_calls: list[tuple[Any, Any]] = []
        self.cancel_calls: list[int] = []
        self.open_calls: list[bool] = []
        self.completed_calls: list[int] = []
        self.execution_filters: list[dict[str, Any] | None] = []
        self.disconnect_calls = 0

    async def connect(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.connected = True

    def isConnected(self) -> bool:  # noqa: N802 - matches ibapi naming
        return self.connected

    async def wait_until_ready(self) -> None:
        return None

    async def disconnect(self) -> None:
        self.connected = False
        self.disconnect_calls += 1

    async def place_order(self, contract: Any, order: Any) -> OrderStatusEvent:
        self.place_calls.append((contract, order))
        return OrderStatusEvent(
            order_id=1,
            status="Submitted",
            filled=Decimal("0"),
            remaining=Decimal("1"),
            avg_fill_price=Decimal("0"),
        )

    async def cancel_order(self, order_id: int) -> OrderStatusEvent:
        self.cancel_calls.append(order_id)
        return OrderStatusEvent(
            order_id=order_id,
            status="Cancelled",
            filled=Decimal("0"),
            remaining=Decimal("1"),
            avg_fill_price=Decimal("0"),
        )

    async def fetch_open_orders(self, *, include_all_accounts: bool) -> list[dict[str, Any]]:
        self.open_calls.append(include_all_accounts)
        return [
            {"order_id": 1, "symbol": "AAPL", "state": {"status": "Submitted"}},
        ]

    async def fetch_completed_orders(self, days_back: int) -> list[dict[str, Any]]:
        self.completed_calls.append(days_back)
        return [
            {"order_id": 2, "symbol": "MSFT", "state": {"completed_status": "filled"}},
        ]

    async def fetch_executions(self, filters: dict[str, Any] | None) -> list[dict[str, Any]]:
        self.execution_filters.append(filters)
        return [
            {
                "execution": ExecutionEvent(
                    exec_id="1",
                    order_id=1,
                    client_id=123,
                    perm_id=999,
                    symbol="AAPL",
                    security_type="STK",
                    exchange="SMART",
                    side="BUY",
                    shares=Decimal("1"),
                    price=Decimal("150"),
                    cumulative_quantity=Decimal("1"),
                    average_price=Decimal("150"),
                )
            }
        ]


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    _state.redis = None
    _state.ib_client = None
    _state.publisher = None
    _state.event_task = None
    _state.shutdown_event = None
    _state.subscription_lock = None
    _state.subscription_count = 0
    _state.last_error = None


@pytest.fixture()
def _stub_environment(monkeypatch: pytest.MonkeyPatch) -> _StubOrdersClient:
    stub_client = _StubOrdersClient()
    stub_publisher = _StubPublisher()

    async def _fake_get_redis_client(*_: Any, **__: Any) -> Any:
        return object()

    async def _fake_update_service_health(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr("services.orders.main.get_redis_client", _fake_get_redis_client)
    monkeypatch.setattr("services.orders.main.update_service_health", _fake_update_service_health)
    monkeypatch.setattr("services.orders.main.RedisPublisher", lambda *args, **kwargs: stub_publisher)
    monkeypatch.setattr("services.orders.main.ServiceIBClient", lambda: stub_client)
    monkeypatch.setattr("services.orders.main._ibapi_available", lambda: False)
    monkeypatch.setattr("services.orders.main._build_contract", lambda payload: payload.model_dump())
    monkeypatch.setattr("services.orders.main._build_order", lambda payload: payload.model_dump())

    return stub_client


def test_orders_subscription_flow(_stub_environment: _StubOrdersClient) -> None:
    stub_client = _stub_environment

    async def _fake_publish_order_event(publisher: _StubPublisher, payload: Any) -> None:
        publisher.messages.append(("orders", payload))
        publisher.signal.set()

    async def _fake_publish_execution_event(publisher: _StubPublisher, payload: Any) -> None:
        publisher.messages.append(("executions", payload))
        publisher.signal.set()

    from services.orders import main as orders_main

    orders_main.publish_order_event = _fake_publish_order_event
    orders_main.publish_execution_event = _fake_publish_execution_event

    with TestClient(app) as client:
        response = client.post("/subscriptions/orders", json={})
        assert response.status_code == 200
        payload = response.json()
        assert payload["subscription_count"] == 1

        assert stub_client.loop is not None
        status_event = OrderStatusEvent(
            order_id=1,
            status="Submitted",
            filled=Decimal("0"),
            remaining=Decimal("1"),
            avg_fill_price=Decimal("0"),
        )
        future = asyncio.run_coroutine_threadsafe(
            stub_client.callback_queue.put(("order_status", status_event)),
            stub_client.loop,
        )
        future.result(timeout=1)
        assert orders_main._state.publisher is not None
        stub_publisher = orders_main._state.publisher  # type: ignore[assignment]
        assert stub_publisher.signal.wait(timeout=1)
        assert stub_publisher.messages
        channel, message = stub_publisher.messages[0]
        assert channel == "orders"
        assert isinstance(message, OrderStatusEvent)

        response = client.delete("/subscriptions/orders")
        assert response.status_code == 200
        payload = response.json()
        assert payload["subscription_count"] == 0
        assert stub_client.disconnect_calls == 1


def test_order_endpoints_cover_core_flows(_stub_environment: _StubOrdersClient) -> None:
    stub_client = _stub_environment

    with TestClient(app) as client:
        place_response = client.post(
            "/orders",
            json={
                "contract": {"symbol": "AAPL", "secType": "STK", "exchange": "SMART"},
                "order": {"action": "BUY", "totalQuantity": 1, "orderType": "MARKET"},
            },
        )
        assert place_response.status_code == 201
        assert stub_client.place_calls

        cancel_response = client.delete("/orders/1")
        assert cancel_response.status_code == 200
        assert stub_client.cancel_calls == [1]

        open_response = client.get("/orders/open", params={"include_all_accounts": True})
        assert open_response.status_code == 200
        assert stub_client.open_calls == [True]

        completed_response = client.get("/orders/completed", params={"days_back": 5})
        assert completed_response.status_code == 200
        assert stub_client.completed_calls == [5]

        executions_response = client.get(
            "/orders/executions",
            params={"acctCode": "DU1", "symbol": "AAPL", "side": "BUY"},
        )
        assert executions_response.status_code == 200
        assert stub_client.execution_filters[-1] == {
            "acctCode": "DU1",
            "symbol": "AAPL",
            "side": "BUY",
        }
