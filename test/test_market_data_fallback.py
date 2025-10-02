from typing import Any, Mapping

import pytest

from src.routes import market_data as market_data_module


class FakeMarketDataClient:
    def __init__(self) -> None:
        self.chart_payload: Mapping[str, Any] | None = None
        self.historical_payload: Mapping[str, Any] | None = None
        self.calls: list[tuple[str, Mapping[str, Any]]] = []

    async def request_chart_data(self, payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
        self.calls.append(("chart", dict(payload)))
        return self.chart_payload

    async def request_historical_data(self, payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
        self.calls.append(("historical", dict(payload)))
        return self.historical_payload


@pytest.fixture(autouse=True)
def _reset_market_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(market_data_module, "historical_data_cache", {})

    if hasattr(market_data_module, "_get_data_layer_manager"):
        def _fail_if_data_layer_accessed() -> None:
            raise AssertionError("tests should not access the legacy data layer")

        monkeypatch.setattr(
            market_data_module,
            "_get_data_layer_manager",
            _fail_if_data_layer_accessed,
        )


@pytest.fixture()
def fake_market_service(monkeypatch: pytest.MonkeyPatch) -> FakeMarketDataClient:
    client = FakeMarketDataClient()
    monkeypatch.setattr(market_data_module, "_MARKET_DATA_SERVICE_CLIENT", client)
    monkeypatch.setattr(market_data_module, "_get_market_data_service_client", lambda: client)
    return client


def test_chart_data_relies_on_service(client, admin_headers, fake_market_service: FakeMarketDataClient):
    fake_market_service.chart_payload = {
        "success": True,
        "data": [
            {
                "time": "2024-01-02T10:30:00-05:00",
                "open": 100.0,
                "high": 110.0,
                "low": 95.0,
                "close": 108.0,
                "volume": 1234,
            }
        ],
        "symbol": "TESTFALLBACK",
        "duration": "1 D",
        "bar_size": "1 min",
        "source": "market_data_service",
    }

    response = client.post(
        "/api/chart_data",
        json={
            "symbol": "TESTFALLBACK",
            "duration": "1 D",
            "bar_size": "1 min",
            "exchange": "SMART",
        },
        headers=admin_headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["data"][0]["close"] == 108.0
    assert payload["source"] == "market_data_service"
    assert fake_market_service.calls and fake_market_service.calls[0][0] == "chart"


def test_chart_data_returns_404_when_service_empty(client, admin_headers, fake_market_service: FakeMarketDataClient):
    fake_market_service.chart_payload = {"data": None}

    response = client.post(
        "/api/chart_data",
        json={
            "symbol": "EMPTY",
            "duration": "1 D",
            "bar_size": "1 min",
            "exchange": "SMART",
        },
        headers=admin_headers,
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "No chart data found"
