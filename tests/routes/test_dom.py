from __future__ import annotations

from typing import Any, Mapping

import pytest
from fastapi import FastAPI, Request, status
from fastapi.testclient import TestClient

from src.routes import dom as dom_module
from src.routes.service_clients import ServiceUnavailableError


class StubDomServiceClient:
    """Stub for DOM service client."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.alerts_response: Mapping[str, Any] = {"alerts": []}
        self.config_response: Mapping[str, Any] = {"config": {"enabled": True}}
        self.data_response: Mapping[str, Any] = {"data": {"bids": [], "asks": []}}
        self.history_response: Mapping[str, Any] = {"history": []}
        self.stats_response: Mapping[str, Any] = {"stats": {"volume": 1000}}
        self.should_fail: bool = False

    async def get_alerts(self) -> Mapping[str, Any]:
        self.calls.append(("get_alerts", None))
        if self.should_fail:
            raise ServiceUnavailableError("DOM service unavailable")
        return self.alerts_response

    async def get_config(self) -> Mapping[str, Any]:
        self.calls.append(("get_config", None))
        if self.should_fail:
            raise ServiceUnavailableError("DOM service unavailable")
        return self.config_response

    async def get_data(self) -> Mapping[str, Any]:
        self.calls.append(("get_data", None))
        if self.should_fail:
            raise ServiceUnavailableError("DOM service unavailable")
        return self.data_response

    async def get_history(self, symbol: str) -> Mapping[str, Any]:
        self.calls.append(("get_history", symbol))
        if self.should_fail:
            raise ServiceUnavailableError("DOM service unavailable")
        return self.history_response

    async def get_stats(self, symbol: str) -> Mapping[str, Any]:
        self.calls.append(("get_stats", symbol))
        if self.should_fail:
            raise ServiceUnavailableError("DOM service unavailable")
        return self.stats_response

    async def acknowledge_alert(self, alert_id: str) -> Mapping[str, Any]:
        self.calls.append(("acknowledge_alert", alert_id))
        if self.should_fail:
            raise ServiceUnavailableError("DOM service unavailable")
        return {"success": True}

    async def start_dom(self) -> Mapping[str, Any]:
        self.calls.append(("start_dom", None))
        if self.should_fail:
            raise ServiceUnavailableError("DOM service unavailable")
        return {"success": True, "status": "started"}

    async def stop_dom(self) -> Mapping[str, Any]:
        self.calls.append(("stop_dom", None))
        if self.should_fail:
            raise ServiceUnavailableError("DOM service unavailable")
        return {"success": True, "status": "stopped"}

    async def update_config(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append(("update_config", payload))
        if self.should_fail:
            raise ServiceUnavailableError("DOM service unavailable")
        return {"success": True}


@pytest.fixture()
def dom_app(monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, StubDomServiceClient]:
    """Create a FastAPI app with DOM routes for testing."""
    app = FastAPI()
    app.include_router(dom_module.router)

    async def allow_user(request: Request) -> str:
        request.state.is_read_only = False
        request.state.user_role = "admin"
        request.state.username = "tester"
        return "tester"

    app.dependency_overrides[dom_module.get_current_user] = allow_user

    stub_client = StubDomServiceClient()
    monkeypatch.setattr(dom_module, "get_dom_service_client", lambda: stub_client)

    return app, stub_client


def test_get_alerts_returns_alerts_list(dom_app: tuple[FastAPI, StubDomServiceClient]) -> None:
    """Test that /api/alerts returns list of alerts."""
    app, stub_client = dom_app
    client = TestClient(app)

    response = client.get("/api/alerts")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert "alerts" in data
    assert stub_client.calls == [("get_alerts", None)]


def test_get_alerts_returns_503_when_service_unavailable(dom_app: tuple[FastAPI, StubDomServiceClient]) -> None:
    """Test that /api/alerts returns 503 when service is unavailable."""
    app, stub_client = dom_app
    stub_client.should_fail = True
    client = TestClient(app)

    response = client.get("/api/alerts")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_get_dom_config_returns_configuration(dom_app: tuple[FastAPI, StubDomServiceClient]) -> None:
    """Test that /api/config/dom returns DOM configuration."""
    app, stub_client = dom_app
    client = TestClient(app)

    response = client.get("/api/config/dom")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert "config" in data
    assert stub_client.calls == [("get_config", None)]


def test_get_dom_config_returns_503_when_service_unavailable(dom_app: tuple[FastAPI, StubDomServiceClient]) -> None:
    """Test that /api/config/dom returns 503 when service is unavailable."""
    app, stub_client = dom_app
    stub_client.should_fail = True
    client = TestClient(app)

    response = client.get("/api/config/dom")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_get_dom_data_returns_market_data(dom_app: tuple[FastAPI, StubDomServiceClient]) -> None:
    """Test that /api/dom/data returns DOM market data."""
    app, stub_client = dom_app
    client = TestClient(app)

    response = client.get("/api/dom/data")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert "data" in data
    assert stub_client.calls == [("get_data", None)]


def test_get_dom_data_returns_503_when_service_unavailable(dom_app: tuple[FastAPI, StubDomServiceClient]) -> None:
    """Test that /api/dom/data returns 503 when service is unavailable."""
    app, stub_client = dom_app
    stub_client.should_fail = True
    client = TestClient(app)

    response = client.get("/api/dom/data")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_get_dom_history_returns_symbol_history(dom_app: tuple[FastAPI, StubDomServiceClient]) -> None:
    """Test that /api/dom/history/{symbol} returns historical data for symbol."""
    app, stub_client = dom_app
    client = TestClient(app)

    response = client.get("/api/dom/history/AAPL")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert "history" in data
    assert stub_client.calls == [("get_history", "AAPL")]


def test_get_dom_history_returns_503_when_service_unavailable(dom_app: tuple[FastAPI, StubDomServiceClient]) -> None:
    """Test that /api/dom/history/{symbol} returns 503 when service is unavailable."""
    app, stub_client = dom_app
    stub_client.should_fail = True
    client = TestClient(app)

    response = client.get("/api/dom/history/AAPL")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_get_dom_stats_returns_symbol_stats(dom_app: tuple[FastAPI, StubDomServiceClient]) -> None:
    """Test that /api/dom/stats/{symbol} returns statistics for symbol."""
    app, stub_client = dom_app
    client = TestClient(app)

    response = client.get("/api/dom/stats/AAPL")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert "stats" in data
    assert stub_client.calls == [("get_stats", "AAPL")]


def test_get_dom_stats_returns_503_when_service_unavailable(dom_app: tuple[FastAPI, StubDomServiceClient]) -> None:
    """Test that /api/dom/stats/{symbol} returns 503 when service is unavailable."""
    app, stub_client = dom_app
    stub_client.should_fail = True
    client = TestClient(app)

    response = client.get("/api/dom/stats/AAPL")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_acknowledge_alert_calls_service(dom_app: tuple[FastAPI, StubDomServiceClient]) -> None:
    """Test that POST /api/alerts/{alert_id}/ack acknowledges an alert."""
    app, stub_client = dom_app
    client = TestClient(app)

    response = client.post("/api/alerts/alert123/ack")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["success"] is True
    assert stub_client.calls == [("acknowledge_alert", "alert123")]


def test_acknowledge_alert_returns_503_when_service_unavailable(dom_app: tuple[FastAPI, StubDomServiceClient]) -> None:
    """Test that POST /api/alerts/{alert_id}/ack returns 503 when service is unavailable."""
    app, stub_client = dom_app
    stub_client.should_fail = True
    client = TestClient(app)

    response = client.post("/api/alerts/alert123/ack")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_start_dom_calls_service(dom_app: tuple[FastAPI, StubDomServiceClient]) -> None:
    """Test that POST /api/dom/start starts the DOM service."""
    app, stub_client = dom_app
    client = TestClient(app)

    response = client.post("/api/dom/start")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["success"] is True
    assert data["status"] == "started"
    assert stub_client.calls == [("start_dom", None)]


def test_start_dom_returns_503_when_service_unavailable(dom_app: tuple[FastAPI, StubDomServiceClient]) -> None:
    """Test that POST /api/dom/start returns 503 when service is unavailable."""
    app, stub_client = dom_app
    stub_client.should_fail = True
    client = TestClient(app)

    response = client.post("/api/dom/start")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_stop_dom_calls_service(dom_app: tuple[FastAPI, StubDomServiceClient]) -> None:
    """Test that POST /api/dom/stop stops the DOM service."""
    app, stub_client = dom_app
    client = TestClient(app)

    response = client.post("/api/dom/stop")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["success"] is True
    assert data["status"] == "stopped"
    assert stub_client.calls == [("stop_dom", None)]


def test_stop_dom_returns_503_when_service_unavailable(dom_app: tuple[FastAPI, StubDomServiceClient]) -> None:
    """Test that POST /api/dom/stop returns 503 when service is unavailable."""
    app, stub_client = dom_app
    stub_client.should_fail = True
    client = TestClient(app)

    response = client.post("/api/dom/stop")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_update_dom_config_calls_service(dom_app: tuple[FastAPI, StubDomServiceClient]) -> None:
    """Test that PUT /api/config/dom updates DOM configuration."""
    app, stub_client = dom_app
    client = TestClient(app)

    payload = {"enabled": True, "refresh_rate": 1000}
    response = client.put("/api/config/dom", json=payload)

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["success"] is True
    assert stub_client.calls == [("update_config", payload)]


def test_update_dom_config_returns_503_when_service_unavailable(dom_app: tuple[FastAPI, StubDomServiceClient]) -> None:
    """Test that PUT /api/config/dom returns 503 when service is unavailable."""
    app, stub_client = dom_app
    stub_client.should_fail = True
    client = TestClient(app)

    payload = {"enabled": True, "refresh_rate": 1000}
    response = client.put("/api/config/dom", json=payload)

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE