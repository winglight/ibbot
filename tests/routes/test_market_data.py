from __future__ import annotations

from typing import Any, Mapping

import pytest
from fastapi import FastAPI, Request, status
from fastapi.testclient import TestClient

from src.routes import market_data as market_data_module
from src.routes.service_clients import ServiceUnavailableError


class StubMarketDataServiceClient:
    """Stub for market data service client."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.quotes_response: Mapping[str, Any] = {
            "quotes": {
                "AAPL": {
                    "symbol": "AAPL",
                    "bid": 150.25,
                    "ask": 150.30,
                    "last": 150.28,
                    "volume": 1000000,
                    "timestamp": "2024-01-15T10:00:00Z"
                }
            }
        }
        self.historical_response: Mapping[str, Any] = {
            "data": [
                {
                    "timestamp": "2024-01-15T09:30:00Z",
                    "open": 150.00,
                    "high": 150.50,
                    "low": 149.80,
                    "close": 150.28,
                    "volume": 100000
                }
            ]
        }
        self.symbols_response: Mapping[str, Any] = {
            "symbols": ["AAPL", "GOOGL", "MSFT", "TSLA"]
        }
        self.backfill_response: Mapping[str, Any] = {
            "rows": [
                {
                    "status": "queued",
                    "symbol": "AAPL",
                }
            ],
            "message": "Backfill queued",
        }
        self.should_fail: bool = False

    async def get_quotes(self, symbols: list[str]) -> Mapping[str, Any]:
        self.calls.append(("get_quotes", {"symbols": symbols}))
        if self.should_fail:
            raise ServiceUnavailableError("Market data service unavailable")
        return self.quotes_response

    async def get_historical_data(self, symbol: str, **kwargs: Any) -> Mapping[str, Any]:
        self.calls.append(("get_historical_data", {"symbol": symbol, **kwargs}))
        if self.should_fail:
            raise ServiceUnavailableError("Market data service unavailable")
        return self.historical_response

    async def get_available_symbols(self) -> Mapping[str, Any]:
        self.calls.append(("get_available_symbols", None))
        if self.should_fail:
            raise ServiceUnavailableError("Market data service unavailable")
        return self.symbols_response

    async def subscribe_to_quotes(self, symbols: list[str]) -> Mapping[str, Any]:
        self.calls.append(("subscribe_to_quotes", {"symbols": symbols}))
        if self.should_fail:
            raise ServiceUnavailableError("Market data service unavailable")
        return {"status": "subscribed", "symbols": symbols}

    async def unsubscribe_from_quotes(self, symbols: list[str]) -> Mapping[str, Any]:
        self.calls.append(("unsubscribe_from_quotes", {"symbols": symbols}))
        if self.should_fail:
            raise ServiceUnavailableError("Market data service unavailable")
        return {"status": "unsubscribed", "symbols": symbols}

    async def request_backfill(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append(("request_backfill", dict(payload)))
        if self.should_fail:
            raise ServiceUnavailableError("Market data service unavailable")
        return self.backfill_response


@pytest.fixture()
def market_data_app(monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, StubMarketDataServiceClient]:
    """Create a FastAPI app with market data routes for testing."""
    app = FastAPI()
    app.include_router(market_data_module.router)

    async def allow_user(request: Request) -> str:
        request.state.is_read_only = False
        request.state.user_role = "admin"
        request.state.username = "tester"
        return "tester"

    app.dependency_overrides[market_data_module.get_current_user] = allow_user

    stub_client = StubMarketDataServiceClient()
    monkeypatch.setattr(market_data_module, "get_market_data_service_client", lambda: stub_client)

    return app, stub_client


def test_get_quotes_returns_market_data(market_data_app: tuple[FastAPI, StubMarketDataServiceClient]) -> None:
    """Test that /api/market-data/quotes returns quote data."""
    app, stub_client = market_data_app
    client = TestClient(app)

    response = client.get("/api/market-data/quotes?symbols=AAPL,GOOGL")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert "quotes" in data
    assert "AAPL" in data["quotes"]
    quote = data["quotes"]["AAPL"]
    assert "symbol" in quote
    assert "bid" in quote
    assert "ask" in quote
    assert "last" in quote
    assert stub_client.calls == [("get_quotes", {"symbols": ["AAPL", "GOOGL"]})]


def test_get_quotes_returns_503_when_service_unavailable(market_data_app: tuple[FastAPI, StubMarketDataServiceClient]) -> None:
    """Test that /api/market-data/quotes returns 503 when service is unavailable."""
    app, stub_client = market_data_app
    stub_client.should_fail = True
    client = TestClient(app)

    response = client.get("/api/market-data/quotes?symbols=AAPL")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_get_historical_data_returns_price_history(market_data_app: tuple[FastAPI, StubMarketDataServiceClient]) -> None:
    """Test that /api/market-data/historical/{symbol} returns historical data."""
    app, stub_client = market_data_app
    client = TestClient(app)

    response = client.get("/api/market-data/historical/AAPL")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert "data" in data
    assert len(data["data"]) > 0
    
    historical_point = data["data"][0]
    assert "timestamp" in historical_point
    assert "open" in historical_point
    assert "high" in historical_point
    assert "low" in historical_point
    assert "close" in historical_point
    assert "volume" in historical_point
    assert stub_client.calls == [("get_historical_data", {"symbol": "AAPL"})]


def test_get_historical_data_with_parameters(market_data_app: tuple[FastAPI, StubMarketDataServiceClient]) -> None:
    """Test that /api/market-data/historical/{symbol} accepts query parameters."""
    app, stub_client = market_data_app
    client = TestClient(app)

    response = client.get("/api/market-data/historical/AAPL?period=1d&interval=1m&start_date=2024-01-15")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert "data" in data
    
    # Check that parameters were passed to the service
    assert len(stub_client.calls) == 1
    call_name, call_params = stub_client.calls[0]
    assert call_name == "get_historical_data"
    assert call_params["symbol"] == "AAPL"
    assert "period" in call_params
    assert "interval" in call_params
    assert "start_date" in call_params


def test_get_historical_data_returns_503_when_service_unavailable(market_data_app: tuple[FastAPI, StubMarketDataServiceClient]) -> None:
    """Test that /api/market-data/historical/{symbol} returns 503 when service is unavailable."""
    app, stub_client = market_data_app
    stub_client.should_fail = True
    client = TestClient(app)

    response = client.get("/api/market-data/historical/AAPL")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_get_symbols_returns_available_symbols(market_data_app: tuple[FastAPI, StubMarketDataServiceClient]) -> None:
    """Test that /api/market-data/symbols returns list of available symbols."""
    app, stub_client = market_data_app
    client = TestClient(app)

    response = client.get("/api/market-data/symbols")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert "symbols" in data
    assert len(data["symbols"]) == 4
    assert "AAPL" in data["symbols"]
    assert "GOOGL" in data["symbols"]
    assert stub_client.calls == [("get_available_symbols", None)]


def test_get_symbols_returns_503_when_service_unavailable(market_data_app: tuple[FastAPI, StubMarketDataServiceClient]) -> None:
    """Test that /api/market-data/symbols returns 503 when service is unavailable."""
    app, stub_client = market_data_app
    stub_client.should_fail = True
    client = TestClient(app)

    response = client.get("/api/market-data/symbols")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_api_market_backfill_forwards_request(
    market_data_app: tuple[FastAPI, StubMarketDataServiceClient]
) -> None:
    """The API alias should invoke the market data service backfill endpoint."""

    app, stub_client = market_data_app
    client = TestClient(app)

    payload = {
        "symbol": "AAPL",
        "duration": "1 D",
        "barSize": "1 min",
        "exchange": "SMART",
    }

    response = client.post("/api/market/backfill", json=payload)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["success"] is True
    assert body["symbol"] == "AAPL"
    assert body["interval"] == "1 min"
    assert stub_client.calls == [("request_backfill", payload)]


def test_subscribe_to_quotes_creates_subscription(market_data_app: tuple[FastAPI, StubMarketDataServiceClient]) -> None:
    """Test that POST /api/market-data/quotes/subscribe creates quote subscription."""
    app, stub_client = market_data_app
    client = TestClient(app)

    response = client.post("/api/market-data/quotes/subscribe", json={"symbols": ["AAPL", "GOOGL"]})

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["status"] == "subscribed"
    assert "AAPL" in data["symbols"]
    assert "GOOGL" in data["symbols"]
    assert stub_client.calls == [("subscribe_to_quotes", {"symbols": ["AAPL", "GOOGL"]})]


def test_subscribe_to_quotes_returns_503_when_service_unavailable(market_data_app: tuple[FastAPI, StubMarketDataServiceClient]) -> None:
    """Test that POST /api/market-data/quotes/subscribe returns 503 when service is unavailable."""
    app, stub_client = market_data_app
    stub_client.should_fail = True
    client = TestClient(app)

    response = client.post("/api/market-data/quotes/subscribe", json={"symbols": ["AAPL"]})

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_unsubscribe_from_quotes_removes_subscription(market_data_app: tuple[FastAPI, StubMarketDataServiceClient]) -> None:
    """Test that POST /api/market-data/quotes/unsubscribe removes quote subscription."""
    app, stub_client = market_data_app
    client = TestClient(app)

    response = client.post("/api/market-data/quotes/unsubscribe", json={"symbols": ["AAPL", "GOOGL"]})

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["status"] == "unsubscribed"
    assert "AAPL" in data["symbols"]
    assert "GOOGL" in data["symbols"]
    assert stub_client.calls == [("unsubscribe_from_quotes", {"symbols": ["AAPL", "GOOGL"]})]


def test_unsubscribe_from_quotes_returns_503_when_service_unavailable(market_data_app: tuple[FastAPI, StubMarketDataServiceClient]) -> None:
    """Test that POST /api/market-data/quotes/unsubscribe returns 503 when service is unavailable."""
    app, stub_client = market_data_app
    stub_client.should_fail = True
    client = TestClient(app)

    response = client.post("/api/market-data/quotes/unsubscribe", json={"symbols": ["AAPL"]})

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_quotes_response_format(market_data_app: tuple[FastAPI, StubMarketDataServiceClient]) -> None:
    """Test the format of the quotes response."""
    app, stub_client = market_data_app
    client = TestClient(app)

    response = client.get("/api/market-data/quotes?symbols=AAPL")

    assert response.status_code == status.HTTP_200_OK
    assert response.headers["content-type"] == "application/json"
    
    data = response.json()
    assert isinstance(data["quotes"], dict)
    
    # Check quote format
    if data["quotes"]:
        symbol, quote = next(iter(data["quotes"].items()))
        assert isinstance(quote["bid"], (int, float))
        assert isinstance(quote["ask"], (int, float))
        assert isinstance(quote["last"], (int, float))
        assert isinstance(quote["volume"], int)
        assert isinstance(quote["timestamp"], str)