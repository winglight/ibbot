import os
from datetime import datetime
import sys
import types
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("IBBOT_SKIP_SERVER", "1")

# Provide lightweight stubs for optional runtime dependencies when they are not
# available in the test environment (e.g., CI without IBKR libraries).
if 'ib_insync' not in sys.modules:
    ib_module = types.ModuleType('ib_insync')

    class _DummyIB:
        def __init__(self, *args, **kwargs):
            pass

        def isConnected(self):
            return False

    ib_module.IB = _DummyIB
    ib_module.util = types.SimpleNamespace()
    ib_module.ExecutionFilter = type('ExecutionFilter', (), {})
    ib_module.Stock = type('Stock', (), {})
    ib_module.Future = type('Future', (), {})
    ib_module.Order = type('Order', (), {})
    ib_module.MarketOrder = type('MarketOrder', (), {})
    ib_module.LimitOrder = type('LimitOrder', (), {})
    ib_module.Contract = type('Contract', (), {})

    sys.modules['ib_insync'] = ib_module

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
        self._cached_account_info = dict(self.account_info)
        self._cached_account_info_timestamp = datetime.utcnow()
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
    stub_risk_manager = SimpleNamespace(equity_last=0.0, sod_equity=0.0)

    class StubDBManager:
        def __init__(self):
            self.orders = []

        def fetch_orders_for_pnl(self):
            return list(self.orders)

    stub_db = StubDBManager()

    monkeypatch.setattr(core, "connection_manager", connection_manager)
    monkeypatch.setattr(core, "ibkr_client", ibkr_client)
    monkeypatch.setattr(core, "risk_manager", stub_risk_manager, raising=False)
    monkeypatch.setattr(connection_routes, "connection_manager", connection_manager)
    monkeypatch.setattr(account_routes, "connection_manager", connection_manager)
    monkeypatch.setattr(account_routes, "core", SimpleNamespace(ibkr_client=ibkr_client, risk_manager=stub_risk_manager))
    monkeypatch.setattr(account_routes, "get_db_manager", lambda: stub_db)

    token, _ = create_access_token({"sub": "tester"})
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app)
    return client, headers, connection_manager, ibkr_client, stub_db, stub_risk_manager


def test_login_and_connection_status(api_client):
    client, headers, connection_manager, *_ = api_client

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
    client, headers, connection_manager, ibkr_client, *_ = api_client

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


def test_daily_summary_surplus_uses_cache(api_client, monkeypatch):
    client, headers, connection_manager, ibkr_client, stub_db, stub_risk_manager = api_client

    # Prepare cached snapshot and make live fetch fail
    now = datetime.utcnow()
    iso_now = now.isoformat()
    cached_snapshot = {
        "account_id": "DUCACHED",
        "account_type": "Paper",
        "net_liquidation": 48436.63,
        "available_funds": 40000.0,
        "buying_power": 190000.0,
        "unrealized_pnl": 0.0,
        "realized_pnl": 0.0,
    }
    ibkr_client._cached_account_info = dict(cached_snapshot)
    ibkr_client._cached_account_info_timestamp = now

    async def failing_account_info():
        raise RuntimeError("simulated IBKR failure")

    monkeypatch.setattr(ibkr_client, "get_account_info", failing_account_info)

    # Provide a single order so PnL aggregation works
    stub_db.orders = [
        {
            "order_id": "TEST-1",
            "created_at": iso_now,
            "executed_at": iso_now,
            "pnl": 150.0,
        }
    ]

    # Ensure risk manager fallback stays unused in this scenario
    stub_risk_manager.equity_last = 0.0
    stub_risk_manager.sod_equity = 0.0

    resp = client.get("/api/account/daily-summary?days=3", headers=headers)
    assert resp.status_code == 200
    data = resp.json()

    assert data["surplus_source"] == "ibkr_cached"
    assert data["net_liquidation"] == pytest.approx(48436.63, rel=1e-6)
    assert data["base_surplus"] == pytest.approx(48436.63 - 150.0, abs=0.02)
    assert data["net_liquidation_timestamp"] is not None

    # All returned days should have surplus data populated
    for day in data["days"]:
        assert day["starting_surplus"] is not None
        assert day["ending_surplus"] is not None


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
