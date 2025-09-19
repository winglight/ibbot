import os
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("IBBOT_SKIP_SERVER", "1")

import core  # noqa: E402
from src.routes import auth as auth_routes  # noqa: E402
from src.routes import orders as orders_routes  # noqa: E402
from src.routes.auth import create_access_token  # noqa: E402


class StubConnectionManager:
    def __init__(self, connected=True):
        self._connected = connected
        self.config = SimpleNamespace(paper_trading=True)

    def is_connected(self):
        return self._connected

    def set_connected(self, value: bool) -> None:
        self._connected = value


class StubIBKRClient:
    def __init__(self):
        self.market_orders = []
        self.limit_orders = []
        self.cancelled_orders = []

    async def place_market_order(self, *, symbol, quantity, action, exchange, sec_type):
        self.market_orders.append(
            {
                "symbol": symbol,
                "quantity": quantity,
                "action": action,
                "exchange": exchange,
                "sec_type": sec_type,
            }
        )
        return SimpleNamespace(order=SimpleNamespace(orderId=1001))

    def place_limit_order(self, *, symbol, quantity, price, action, exchange, sec_type):
        self.limit_orders.append(
            {
                "symbol": symbol,
                "quantity": quantity,
                "price": price,
                "action": action,
                "exchange": exchange,
                "sec_type": sec_type,
            }
        )
        return SimpleNamespace(order=SimpleNamespace(orderId=2002))

    def cancel_order(self, order_id):
        self.cancelled_orders.append(order_id)
        return True, "Cancelled"


class StubOrderManager:
    def __init__(self):
        self.created = []
        self.cancelled = []

    def create_order(self, **kwargs):
        self.created.append(kwargs)

    def cancel_order(self, order_id, reason=""):
        self.cancelled.append((order_id, reason))
        return True


class StubWebsocketManager:
    def __init__(self):
        self.messages = []

    async def broadcast(self, payload):
        self.messages.append(payload)


@pytest.fixture()
def orders_client(monkeypatch):
    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(orders_routes.router)

    connection = StubConnectionManager()
    ibkr_client = StubIBKRClient()
    order_manager = StubOrderManager()
    websocket_manager = StubWebsocketManager()

    monkeypatch.setattr(core, "connection_manager", connection)
    monkeypatch.setattr(core, "ibkr_client", ibkr_client)
    monkeypatch.setattr(core, "order_manager", order_manager)
    monkeypatch.setattr(core, "websocket_manager", websocket_manager)
    monkeypatch.setattr(
        orders_routes, "connection_manager", connection
    )
    monkeypatch.setattr(orders_routes, "order_manager", order_manager)
    monkeypatch.setattr(orders_routes, "websocket_manager", websocket_manager)

    token, _ = create_access_token({"sub": "tester"})
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app)
    return client, headers, connection, ibkr_client, order_manager, websocket_manager


def test_place_market_order_success(orders_client):
    client, headers, connection, ibkr_client, order_manager, websocket_manager = orders_client

    payload = {
        "symbol": "AAPL",
        "quantity": 5,
        "action": "BUY",
        "order_type": "MARKET",
        "exchange": "SMART",
        "sec_type": "STK",
    }

    resp = client.post("/api/place_order", json=payload, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["order_id"] == 1001

    assert len(ibkr_client.market_orders) == 1
    assert ibkr_client.market_orders[0]["symbol"] == "AAPL"
    assert len(order_manager.created) == 1
    created = order_manager.created[0]
    assert created["symbol"] == "AAPL"
    assert created["order_type"] == "MKT"
    assert created["order_source"] == "manual"

    # two websocket broadcasts (order_placed + order_update)
    assert len(websocket_manager.messages) == 2
    assert websocket_manager.messages[0]["type"] == "order_placed"


def test_place_limit_order_with_risk_rule(orders_client):
    client, headers, connection, ibkr_client, order_manager, websocket_manager = orders_client

    payload = {
        "symbol": "MSFT",
        "quantity": 2,
        "action": "SELL",
        "order_type": "LIMIT",
        "price": 310.5,
        "exchange": "SMART",
        "sec_type": "STK",
        "risk_rule": {
            "symbol": "MSFT",
            "stop_loss_price": 305.0,
            "take_profit_price": 315.0,
        },
    }

    resp = client.post("/api/place_order", json=payload, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["order_id"] == 2002

    assert len(ibkr_client.limit_orders) == 1
    limit_call = ibkr_client.limit_orders[0]
    assert limit_call["symbol"] == "MSFT"
    assert limit_call["price"] == 310.5

    created = order_manager.created[-1]
    assert "pending_risk_rule" in created.get("notes", "")
    assert "SL=305.0" in created["notes"]
    assert "TP=315.0" in created["notes"]


def test_place_order_requires_connection(orders_client):
    client, headers, connection, *_ = orders_client
    connection.set_connected(False)

    payload = {
        "symbol": "AAPL",
        "quantity": 1,
        "action": "BUY",
        "order_type": "MARKET",
    }

    resp = client.post("/api/place_order", json=payload, headers=headers)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Not connected to IBKR"


def test_cancel_order_success(orders_client):
    client, headers, connection, ibkr_client, order_manager, _ = orders_client
    resp = client.delete("/api/cancel_order/12345", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert ibkr_client.cancelled_orders == [12345]
    assert order_manager.cancelled == [("12345", "Cancelled via API")]


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
