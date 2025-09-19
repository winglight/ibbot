import os
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("IBBOT_SKIP_SERVER", "1")

import core  # noqa: E402
from src.routes import auth as auth_routes  # noqa: E402
from src.routes import risk as risk_routes  # noqa: E402
from src.routes.auth import create_access_token  # noqa: E402


class StubRiskRule:
    def __init__(self, symbol, sl=None, tp=None, enabled=True):
        self.symbol = symbol
        self.stop_loss_price = sl
        self.take_profit_price = tp
        self.max_loss_percent = None
        self.max_time_span = 30
        self.enabled = enabled
        self.created_at = datetime(2024, 1, 1)


class StubRiskManager:
    def __init__(self):
        self.rules = {}
        self.queue_size = 0

    def add_risk_rule(self, symbol, stop_loss_price=None, take_profit_price=None, max_loss_percent=None, max_time_span=None):
        self.rules[symbol] = StubRiskRule(symbol, stop_loss_price, take_profit_price, enabled=True)
        self.queue_size += 1

    def get_risk_rules(self):
        return self.rules

    def update_risk_rule(self, symbol, enabled=True):
        rule = self.rules.get(symbol)
        if not rule:
            return False
        rule.enabled = enabled
        return True

    def remove_risk_rule(self, symbol):
        self.rules.pop(symbol, None)

    def get_order_queue_size(self):
        return self.queue_size

    def get_config(self):
        return {"stop_loss_ratio": 0.05, "take_profit_ratio": 0.05}


@pytest.fixture()
def risk_client(monkeypatch):
    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(risk_routes.router)

    stub_risk_manager = StubRiskManager()
    monkeypatch.setattr(core, "risk_manager", stub_risk_manager)
    monkeypatch.setattr(risk_routes, "risk_manager", stub_risk_manager)

    token, _ = create_access_token({"sub": "tester"})
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app)
    return client, headers, stub_risk_manager


def test_risk_rule_lifecycle(risk_client):
    client, headers, stub_risk_manager = risk_client

    payload = {
        "symbol": "AAPL",
        "stop_loss_price": 150.0,
        "take_profit_price": 160.0,
    }

    # add rule
    resp = client.post("/api/risk_rules", json=payload, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert "AAPL" in stub_risk_manager.rules

    # list rules
    resp = client.get("/api/risk_rules", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert "AAPL" in body["rules"]
    assert body["rules"]["AAPL"]["enabled"] is True

    # update status
    resp = client.put("/api/risk_rules/AAPL", params={"enabled": False}, headers=headers)
    assert resp.status_code == 200
    assert stub_risk_manager.rules["AAPL"].enabled is False

    # delete rule
    resp = client.delete("/api/risk_rules/AAPL", headers=headers)
    assert resp.status_code == 200
    assert "AAPL" not in stub_risk_manager.rules


def test_risk_manager_utilities(risk_client):
    client, headers, stub_risk_manager = risk_client

    # queue status reflects internal counter
    stub_risk_manager.queue_size = 3
    resp = client.get("/api/order_queue_status", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["queue_size"] == 3

    # risk config returns expected defaults
    resp = client.get("/api/risk-config", headers=headers)
    assert resp.status_code == 200
    cfg = resp.json()
    assert cfg["stop_loss_ratio"] > 0
    assert cfg["take_profit_ratio"] > 0


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
