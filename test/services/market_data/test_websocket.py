"""Tests for the market data websocket endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from services.market_data.app import create_app
from services.market_data.runtime import MarketDataRuntime


def _create_test_client(runtime: MarketDataRuntime) -> TestClient:
    runtime.startup = AsyncMock()
    runtime.shutdown = AsyncMock()
    app = create_app(runtime=runtime)
    return TestClient(app)


def test_websocket_sends_initial_snapshot_and_updates() -> None:
    runtime = MarketDataRuntime()
    runtime._snapshots = {
        "BTCUSDT": {
            "symbol": "BTCUSDT",
            "price": 100.0,
            "dom": None,
            "account": None,
            "updated_at": None,
            "latency_ms": None,
            "stale": False,
        },
        "ETHUSDT": {
            "symbol": "ETHUSDT",
            "price": 200.0,
            "dom": None,
            "account": None,
            "updated_at": None,
            "latency_ms": None,
            "stale": False,
        },
    }

    with _create_test_client(runtime) as client:
        manager = client.app.state.market_data_ws_manager
        manager.ping_interval = 5.0  # avoid interference for this test
        with client.websocket_connect("/ws/market?symbols=BTCUSDT") as websocket:
            snapshot = websocket.receive_json()
            assert snapshot["type"] == "snapshot"
            assert [item["symbol"] for item in snapshot["data"]] == ["BTCUSDT"]

            runtime._notify_update_listeners(
                {"symbol": "BTCUSDT", "type": "price", "data": {"price": 101.5}}
            )

            update = websocket.receive_json()
            assert update == {
                "symbol": "BTCUSDT",
                "type": "price",
                "data": {"price": 101.5},
            }


def test_websocket_filters_by_event_type() -> None:
    runtime = MarketDataRuntime()
    runtime._snapshots["BTCUSDT"] = {
        "symbol": "BTCUSDT",
        "price": 100.0,
        "dom": None,
        "account": None,
        "updated_at": None,
        "latency_ms": None,
        "stale": False,
    }

    with _create_test_client(runtime) as client:
        manager = client.app.state.market_data_ws_manager
        manager.ping_interval = 0.05
        manager.ping_timeout = 0.2

        with client.websocket_connect("/ws/market?types=price") as websocket:
            snapshot = websocket.receive_json()
            assert snapshot["type"] == "snapshot"

            runtime._notify_update_listeners(
                {"symbol": "BTCUSDT", "type": "price", "data": {"price": 102.0}}
            )

            price_update = websocket.receive_json()
            assert price_update["type"] == "price"

            runtime._notify_update_listeners(
                {"symbol": "BTCUSDT", "type": "dom", "data": {"dom": [1, 2]}}
            )

            # Next message should be a ping because DOM updates are filtered out
            ping = websocket.receive_json()
            assert ping["type"] == "ping"
            websocket.send_json({"type": "pong"})


def test_websocket_disconnects_when_pong_missing() -> None:
    runtime = MarketDataRuntime()
    runtime._snapshots["BTCUSDT"] = {
        "symbol": "BTCUSDT",
        "price": 100.0,
        "dom": None,
        "account": None,
        "updated_at": None,
        "latency_ms": None,
        "stale": False,
    }

    with _create_test_client(runtime) as client:
        manager = client.app.state.market_data_ws_manager
        manager.ping_interval = 0.05
        manager.ping_timeout = 0.05

        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/market") as websocket:
                websocket.receive_json()  # initial snapshot
                ping = websocket.receive_json()
                assert ping["type"] == "ping"
                # Do not respond with pong; expect disconnect on next receive
                websocket.receive_json()
