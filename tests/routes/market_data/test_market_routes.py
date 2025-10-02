from typing import Any, Mapping

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.routes import market_data as market_data_module
from src.routes.service_clients import (
    ServiceResponseError,
    ServiceUnavailableError,
)


@pytest.fixture()
def market_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    app = FastAPI()
    app.include_router(market_data_module.router)

    async def allow_user() -> str:
        return "tester"

    app.dependency_overrides[market_data_module.get_current_user] = allow_user

    monkeypatch.setattr(market_data_module, "_get_data_layer_manager", lambda: None)
    monkeypatch.setattr(market_data_module, "historical_data_cache", {})
    monkeypatch.setattr(market_data_module, "CURRENT_PRICE_CACHE_ONLY", False)

    return app


def _override_market_service(monkeypatch: pytest.MonkeyPatch, client: Any) -> None:
    monkeypatch.setattr(market_data_module, "_MARKET_DATA_SERVICE_CLIENT", client)
    monkeypatch.setattr(market_data_module, "_get_market_data_service_client", lambda: client)


def test_historical_data_returns_service_payload(
    market_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StubClient:
        async def request_historical_data(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
            assert payload["symbol"] == "ES"
            return {
                "data": [
                    {
                        "date": "2024-01-01T09:30:00-05:00",
                        "open": 1,
                        "high": 2,
                        "low": 0,
                        "close": 1.5,
                        "volume": 10,
                    }
                ],
                "source": "service",
            }

    _override_market_service(monkeypatch, StubClient())

    with TestClient(market_app) as client:
        response = client.post(
            "/api/historical_data",
            json={"symbol": "ES", "duration": "1 D", "bar_size": "1 min", "exchange": "CME"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["data"][0]["open"] == 1
    assert payload["source"] == "service"


def test_historical_data_service_unavailable(
    market_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailingClient:
        async def request_historical_data(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
            raise ServiceUnavailableError("market_data", "offline")

    _override_market_service(monkeypatch, FailingClient())

    with TestClient(market_app) as client:
        response = client.post(
            "/api/historical_data",
            json={"symbol": "ES", "duration": "1 D", "bar_size": "1 min", "exchange": "CME"},
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "Market data service unavailable"


def test_realtime_data_success(
    market_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StubClient:
        async def request(self, method: str, path: str) -> Mapping[str, Any]:
            assert method == "GET"
            assert path == "/market/ES"
            return {"symbol": "ES", "price": {"last": 123.45}}

    _override_market_service(monkeypatch, StubClient())

    with TestClient(market_app) as client:
        response = client.get("/api/realtime_data/ES")

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["data"]["price"]["last"] == 123.45


def test_realtime_data_not_found(
    market_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StubClient:
        async def request(self, method: str, path: str) -> Mapping[str, Any]:
            raise ServiceResponseError("market_data", status_code=404, detail="not found")

    _override_market_service(monkeypatch, StubClient())

    with TestClient(market_app) as client:
        response = client.get("/api/realtime_data/ES")

    assert response.status_code == 404
    assert response.json()["detail"] == "No data available"


def test_current_price_returns_service_payload(
    market_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StubClient:
        async def fetch_current_price(self, symbol: str) -> Mapping[str, Any]:
            assert symbol == "ES"
            return {"symbol": symbol, "price": 4321.0}

    _override_market_service(monkeypatch, StubClient())

    with TestClient(market_app) as client:
        response = client.post(
            "/api/current_price",
            json={"symbol": "ES", "exchange": "CME"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["price"] == 4321.0
    assert payload["source"] == "market_data_service"


def test_current_price_service_error(
    market_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StubClient:
        async def fetch_current_price(self, symbol: str) -> Mapping[str, Any]:
            raise ServiceResponseError("market_data", status_code=500, detail="oops")

    _override_market_service(monkeypatch, StubClient())

    with TestClient(market_app) as client:
        response = client.post(
            "/api/current_price",
            json={"symbol": "ES", "exchange": "CME"},
        )

    assert response.status_code == 502
    assert response.json()["detail"] == "Market data service error"
