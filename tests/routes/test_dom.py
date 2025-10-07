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
        return {"success": True, **self.alerts_response}

    async def get_config(self) -> Mapping[str, Any]:
        self.calls.append(("get_config", None))
        if self.should_fail:
            raise ServiceUnavailableError("DOM service unavailable")
        return {"success": True, **self.config_response}

    async def get_data(self, symbol: str | None = None) -> Mapping[str, Any]:
        self.calls.append(("get_data", symbol))
        if self.should_fail:
            raise ServiceUnavailableError("DOM service unavailable")
        return {"success": True, **self.data_response}

    async def get_history(
        self,
        symbol: str,
        *,
        limit: int | None = None,
        start: str | None = None,
        end: str | None = None,
        include_snapshots: bool | None = None,
    ) -> Mapping[str, Any]:
        self.calls.append(
            (
                "get_history",
                {
                    "symbol": symbol,
                    "limit": limit,
                    "start": start,
                    "end": end,
                    "include_snapshots": include_snapshots,
                },
            )
        )
        if self.should_fail:
            raise ServiceUnavailableError("DOM service unavailable")
        return {"success": True, "symbol": symbol, **self.history_response}

    async def get_stats(
        self,
        symbol: str,
        *,
        lookback_seconds: int | None = None,
        start: str | None = None,
        end: str | None = None,
        max_points: int | None = None,
    ) -> Mapping[str, Any]:
        self.calls.append(
            (
                "get_stats",
                {
                    "symbol": symbol,
                    "lookback_seconds": lookback_seconds,
                    "start": start,
                    "end": end,
                    "max_points": max_points,
                },
            )
        )
        if self.should_fail:
            raise ServiceUnavailableError("DOM service unavailable")
        return {"success": True, "symbol": symbol, **self.stats_response}

    async def acknowledge_alert(self, alert_id: int) -> Mapping[str, Any]:
        self.calls.append(("acknowledge_alert", alert_id))
        if self.should_fail:
            raise ServiceUnavailableError("DOM service unavailable")
        return {"success": True}

    async def start_dom(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append(("start_dom", dict(payload)))
        if self.should_fail:
            raise ServiceUnavailableError("DOM service unavailable")
        return {"success": True, "pending": False, "already_running": False}

    async def stop_dom(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append(("stop_dom", dict(payload)))
        if self.should_fail:
            raise ServiceUnavailableError("DOM service unavailable")
        return {"success": True, "pending": False}

    async def update_config(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append(("update_config", dict(payload)))
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
    assert stub_client.calls == [
        (
            "get_history",
            {
                "symbol": "AAPL",
                "limit": 500,
                "start": None,
                "end": None,
                "include_snapshots": True,
            },
        )
    ]


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
    assert stub_client.calls == [
        (
            "get_stats",
            {
                "symbol": "AAPL",
                "lookback_seconds": None,
                "start": None,
                "end": None,
                "max_points": None,
            },
        )
    ]


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

    response = client.post("/api/alerts/123/ack")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["success"] is True
    assert stub_client.calls == [("acknowledge_alert", 123)]


def test_acknowledge_alert_returns_503_when_service_unavailable(dom_app: tuple[FastAPI, StubDomServiceClient]) -> None:
    """Test that POST /api/alerts/{alert_id}/ack returns 503 when service is unavailable."""
    app, stub_client = dom_app
    stub_client.should_fail = True
    client = TestClient(app)

    response = client.post("/api/alerts/123/ack")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_start_dom_calls_service(dom_app: tuple[FastAPI, StubDomServiceClient]) -> None:
    """Test that POST /api/dom/start starts the DOM service."""
    app, stub_client = dom_app
    client = TestClient(app)

    response = client.post("/api/dom/start", json={"symbol": "ES"})

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["success"] is True
    assert data["pending"] is False
    assert stub_client.calls == [
        (
            "start_dom",
            {
                "symbol": "ES",
                "num_rows": 5,
                "force_restart": False,
            },
        )
    ]


def test_start_dom_returns_503_when_service_unavailable(dom_app: tuple[FastAPI, StubDomServiceClient]) -> None:
    """Test that POST /api/dom/start returns 503 when service is unavailable."""
    app, stub_client = dom_app
    stub_client.should_fail = True
    client = TestClient(app)

    response = client.post("/api/dom/start", json={"symbol": "ES"})

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_stop_dom_calls_service(dom_app: tuple[FastAPI, StubDomServiceClient]) -> None:
    """Test that POST /api/dom/stop stops the DOM service."""
    app, stub_client = dom_app
    client = TestClient(app)

    response = client.post("/api/dom/stop", json={"symbol": "ES"})

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["success"] is True
    assert data["pending"] is False
    assert stub_client.calls == [
        (
            "stop_dom",
            {"symbol": "ES"},
        )
    ]


def test_stop_dom_returns_503_when_service_unavailable(dom_app: tuple[FastAPI, StubDomServiceClient]) -> None:
    """Test that POST /api/dom/stop returns 503 when service is unavailable."""
    app, stub_client = dom_app
    stub_client.should_fail = True
    client = TestClient(app)

    response = client.post("/api/dom/stop", json={"symbol": "ES"})

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
