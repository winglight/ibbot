from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.routes.auth import create_access_token
from src.routes import strategy as strategy_routes


class FailingStrategyManager:
    def __init__(self):
        self.state = {}

    async def set_data_push(self, name, enabled, subscription=None, symbol=None):  # noqa: D401 - signature mirrors real method
        payload = {
            "enabled": bool(enabled),
            "active": False,
            "subscription": subscription,
            "symbol": symbol,
            "awaiting_data": False,
            "last_data_timestamp": None,
        }
        self.state[name] = payload
        return {"success": False, "error": "Test failure"}

    def get_data_push_state(self, name):
        return self.state.get(name, {})


class SuccessfulStrategyManager:
    def __init__(self):
        self.state = {}

    async def set_data_push(self, name, enabled, subscription=None, symbol=None):  # noqa: D401 - signature mirrors real method
        payload = {
            "success": True,
            "active": bool(enabled),
            "subscription": subscription,
            "symbol": symbol,
            "awaiting_data": True,
            "last_data_timestamp": "2024-01-01T00:00:00",
        }
        self.state[name] = payload
        return payload

    def get_data_push_state(self, name):
        return self.state.get(name, {})


def _build_client(monkeypatch, manager):
    monkeypatch.setattr(strategy_routes, "strategy_manager", manager)
    import core

    monkeypatch.setattr(core, "strategy_manager", manager)

    app = FastAPI()
    app.include_router(strategy_routes.router)
    token, _ = create_access_token({"sub": "tester"})
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}
    return client, headers


def test_data_push_failure_returns_error_and_state(monkeypatch):
    manager = FailingStrategyManager()
    client, headers = _build_client(monkeypatch, manager)

    response = client.post(
        "/api/strategies/Test/data-push",
        headers=headers,
        json={"enabled": True, "subscription": "dom_trend", "symbol": "MNQ"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is False
    assert payload["error"] == "Test failure"
    state = payload["state"]
    assert state["active"] is False
    assert state["subscription"] == "dom_trend"
    assert state["symbol"] == "MNQ"


def test_data_push_success_returns_state(monkeypatch):
    manager = SuccessfulStrategyManager()
    client, headers = _build_client(monkeypatch, manager)

    response = client.post(
        "/api/strategies/Test/data-push",
        headers=headers,
        json={"enabled": True, "subscription": "dom_trend", "symbol": "MNQ"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    state = payload["state"]
    assert state["active"] is True
    assert state["subscription"] == "dom_trend"
    assert state["symbol"] == "MNQ"
