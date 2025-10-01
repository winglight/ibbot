"""Tests for the market data HTTP routes."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from services.market_data.app import create_app
from services.market_data.runtime import MarketDataRuntime


class DummyRedis:
    async def ping(self) -> bool:
        return True


class DummyPublisher:
    def __init__(self, summary: dict[str, Any]) -> None:
        self._summary = summary

    async def get_summary(self) -> dict[str, Any]:
        return self._summary


def _create_test_app(runtime: MarketDataRuntime) -> TestClient:
    runtime.startup = AsyncMock()
    runtime.shutdown = AsyncMock()
    app = create_app(runtime=runtime)
    return TestClient(app)


def test_get_market_snapshot_returns_data() -> None:
    runtime = MarketDataRuntime()
    runtime._snapshots["BTCUSDT"] = {
        "symbol": "BTCUSDT",
        "price": 100.5,
        "dom": {"bids": [[100.4, 1.2]]},
        "updated_at": "2024-01-01T00:00:00+00:00",
        "latency_ms": 12.3,
        "stale": False,
    }
    runtime._update_account_snapshot(
        account_info={"currency": "USD"},
        positions=[{"symbol": "BTCUSDT", "position": 1}],
        updated_at="2024-01-01T00:00:00+00:00",
        stale=False,
        stale_reason=None,
    )

    with _create_test_app(runtime) as client:
        response = client.get("/market/BTCUSDT")

    assert response.status_code == 200
    payload = response.json()
    assert payload["symbol"] == "BTCUSDT"
    assert payload["price"] == 100.5
    assert payload["account_info"] == {"currency": "USD"}
    assert payload["positions"] == [{"symbol": "BTCUSDT", "position": 1}]
    assert payload["account_stale"] is False


def test_get_market_snapshot_missing_symbol_returns_404() -> None:
    runtime = MarketDataRuntime()

    with _create_test_app(runtime) as client:
        response = client.get("/market/UNKNOWN")

    assert response.status_code == 404
    assert response.json()["detail"] == "Symbol not subscribed"


def test_list_market_snapshots_filters_by_stale_flag() -> None:
    runtime = MarketDataRuntime()
    runtime._snapshots = {
        "BTC": {"symbol": "BTC", "price": 1, "dom": None, "account": None, "updated_at": None, "latency_ms": None, "stale": False},
        "ETH": {"symbol": "ETH", "price": 2, "dom": None, "account": None, "updated_at": None, "latency_ms": None, "stale": True},
    }
    runtime._update_account_snapshot(
        account_info=None,
        positions=None,
        updated_at=None,
        stale=False,
        stale_reason=None,
    )

    with _create_test_app(runtime) as client:
        response = client.get("/market", params={"stale": "true"})

    assert response.status_code == 200
    payload = response.json()
    assert [item["symbol"] for item in payload] == ["ETH"]


def test_health_endpoint_reports_runtime_state() -> None:
    runtime = MarketDataRuntime()
    runtime._snapshots = {
        "BTC": {"symbol": "BTC", "price": 1, "dom": None, "account": None, "updated_at": None, "latency_ms": None, "stale": False},
        "ETH": {"symbol": "ETH", "price": 2, "dom": None, "account": None, "updated_at": None, "latency_ms": None, "stale": False},
    }
    runtime.redis = DummyRedis()
    runtime.stream_publisher = DummyPublisher(
        {
            "events": 5,
            "avg_latency_ms": 2.5,
            "max_latency_ms": 4.0,
            "latency_samples": 2,
            "last_publish_ts_ns": 123,
            "last_error": None,
        }
    )
    runtime.health_state["last_error"] = None
    runtime._update_account_snapshot(
        account_info={"balance": 1},
        positions=[{"symbol": "ETH", "qty": 2}],
        updated_at="2024-01-02T00:00:00+00:00",
        stale=True,
        stale_reason="manual",
    )

    with _create_test_app(runtime) as client:
        response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["subscriptions"] == 2
    assert payload["redis"] == {"connected": True, "error": None}
    assert payload["metrics"]["events"] == 5
    assert payload["health"]["last_error"] is None
    assert payload["health"]["account_snapshot"]["stale"] is True
    assert payload["health"]["account_snapshot"]["stale_reason"] == "manual"


def test_legacy_endpoint_aliases_market_snapshot() -> None:
    runtime = MarketDataRuntime()
    runtime._snapshots["BTCUSDT"] = {
        "symbol": "BTCUSDT",
        "price": 101.0,
        "dom": None,
        "account": None,
        "updated_at": None,
        "latency_ms": None,
        "stale": False,
    }
    runtime._update_account_snapshot(
        account_info=None,
        positions=None,
        updated_at=None,
        stale=False,
        stale_reason=None,
    )

    with _create_test_app(runtime) as client:
        legacy = client.get("/api/realtime_data/BTCUSDT")
        modern = client.get("/market/BTCUSDT")

    assert legacy.status_code == modern.status_code == 200
    assert legacy.json() == modern.json()
