import os
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("IBBOT_SKIP_SERVER", "1")

import core  # noqa: E402
from src.routes import auth as auth_routes  # noqa: E402
from src.routes import connection as connection_routes  # noqa: E402
from src.routes import account as account_routes  # noqa: E402
from src.routes.auth import create_access_token, ADMIN_USERNAME, ADMIN_PASSWORD  # noqa: E402


class StubConnectionManager:
    def __init__(self):
        self.config = SimpleNamespace(paper_trading=True)
        self._connected = True
        self._last_error = None

    def is_connected(self):
        return self._connected

    def set_connected(self, value: bool):
        self._connected = value

    def get_last_connect_error(self):
        return self._last_error


class StubIBKRClient:
    def __init__(self):
        self.account_info = {
            "account_id": "DU123456",
            "account_type": "Paper",
            "net_liquidation": 100000.0,
            "available_funds": 80000.0,
            "buying_power": 200000.0,
            "unrealized_pnl": 123.45,
            "realized_pnl": 456.78,
        }
        self.positions = [
            {
                "symbol": "AAPL",
                "position": 10,
                "avg_cost": 150.0,
                "marketPrice": 155.0,
            }
        ]

    async def get_account_info(self):
        return self.account_info

    async def get_positions(self, fast=True):
        return self.positions


@pytest.fixture()
def api_client(monkeypatch):
    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(connection_routes.router)
    app.include_router(account_routes.router)

    connection_manager = StubConnectionManager()
    ibkr_client = StubIBKRClient()

    monkeypatch.setattr(core, "connection_manager", connection_manager)
    monkeypatch.setattr(core, "ibkr_client", ibkr_client)
    monkeypatch.setattr(connection_routes, "connection_manager", connection_manager)
    monkeypatch.setattr(account_routes, "connection_manager", connection_manager)
    monkeypatch.setattr(account_routes, "core", SimpleNamespace(ibkr_client=ibkr_client))

    token, _ = create_access_token({"sub": "tester"})
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app)
    return client, headers, connection_manager, ibkr_client


def test_login_and_connection_status(api_client):
    client, headers, connection_manager, _ = api_client

    # Login with default credentials
    resp = client.post("/api/login", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD})
    assert resp.status_code == 200
    data = resp.json()
    assert data["access_token"]
    assert data["role"] == "admin"

    # Use existing token to query connection status
    resp = client.get("/api/connection_status", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"connected": True, "account_type": "Paper"}

    # Simulate disconnected state
    connection_manager.set_connected(False)
    resp = client.get("/api/connection_status", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["connected"] is False


def test_account_info_and_positions(api_client):
    client, headers, connection_manager, ibkr_client = api_client

    # account info when connected
    resp = client.get("/api/account_info", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["account_info"]["account_id"] == ibkr_client.account_info["account_id"]

    # positions when connected
    resp = client.get("/api/positions", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert len(data["positions"]) == 1
    assert data["positions"][0]["symbol"] == "AAPL"

    # when disconnected expect graceful response
    connection_manager.set_connected(False)
    resp = client.get("/api/account_info", headers=headers)
    assert resp.status_code == 400
    resp = client.get("/api/positions", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["positions"] == []


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
