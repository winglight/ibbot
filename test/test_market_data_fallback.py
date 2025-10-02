from typing import Any, Mapping

import pytest

from src.routes import market_data as market_data_module


@pytest.fixture(autouse=True)
def _reset_market_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(market_data_module, "historical_data_cache", {})
    monkeypatch.setattr(market_data_module, "_get_data_layer_manager", lambda: None)


def _override_market_service(monkeypatch: pytest.MonkeyPatch, client: Any) -> None:
    monkeypatch.setattr(market_data_module, "_MARKET_DATA_SERVICE_CLIENT", client)
    monkeypatch.setattr(market_data_module, "_get_market_data_service_client", lambda: client)


def test_chart_data_relies_on_service(monkeypatch: pytest.MonkeyPatch, client, admin_headers):
    class StubClient:
        async def request(self, method: str, path: str, json: Mapping[str, Any]) -> Mapping[str, Any]:
            assert method == "POST"
            assert path == "/chart_data"
            return {
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
                "symbol": json.get("symbol"),
                "duration": json.get("duration"),
                "bar_size": json.get("bar_size"),
                "source": "market_data_service",
            }

    _override_market_service(monkeypatch, StubClient())

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


def test_chart_data_returns_404_when_service_empty(
    monkeypatch: pytest.MonkeyPatch, client, admin_headers
):
    class EmptyClient:
        async def request(self, method: str, path: str, json: Mapping[str, Any]) -> Mapping[str, Any]:
            return {"data": None}

    _override_market_service(monkeypatch, EmptyClient())

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
