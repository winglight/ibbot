from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from src.routes import websocket as websocket_module
from src.routes.service_clients import ServiceUnavailableError


@pytest.fixture()
def websocket_app(monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, Any]:
    app = FastAPI()
    app.include_router(websocket_module.router)

    class StubWebSocketManager:
        def __init__(self) -> None:
            self.connections: list[Any] = []

        async def connect(self, websocket) -> None:
            await websocket.accept()
            self.connections.append(websocket)

        def disconnect(self, websocket) -> None:
            if websocket in self.connections:
                self.connections.remove(websocket)

    stub_manager = StubWebSocketManager()
    monkeypatch.setattr(websocket_module, "_websocket_manager", lambda: stub_manager)

    async def fake_health() -> dict[str, Any]:
        return {
            "connected": True,
            "account_type": "Paper",
            "error_code": None,
            "stale": False,
        }

    monkeypatch.setattr(websocket_module, "_load_ib_command_health", fake_health)

    async def no_account_snapshot() -> None:
        return None

    async def no_positions_snapshot() -> None:
        return None

    monkeypatch.setattr(websocket_module, "read_account_info_snapshot", no_account_snapshot)
    monkeypatch.setattr(websocket_module, "read_positions_snapshot", no_positions_snapshot)

    class StubAccountClient:
        def __init__(self) -> None:
            self.orders_calls: list[tuple[int, int, Any]] = []

        async def fetch_info(self, *, force_refresh: bool = False) -> dict[str, Any]:
            return {"equity": 1000, "currency": "USD"}

        async def fetch_positions(self, *, force_refresh: bool = False, fast: bool = True) -> list[dict[str, Any]]:
            return [{"symbol": "ES", "position": 1}]

        async def fetch_orders(
            self,
            *,
            limit: int,
            offset: int,
            start_date: str | None = None,
        ) -> dict[str, Any]:
            self.orders_calls.append((limit, offset, start_date))
            return {"orders": [{"order_id": "1", "symbol": "ES"}], "total": 1}

    account_client = StubAccountClient()
    monkeypatch.setattr(websocket_module, "_ACCOUNT_CLIENT", account_client)

    class StubRiskClient:
        async def fetch_rules(self) -> dict[str, Any]:
            return {"rules": [{"symbol": "ES", "enabled": True}]}

    monkeypatch.setattr(websocket_module, "_RISK_CLIENT", StubRiskClient())

    class StubStrategyClient:
        async def list_strategies(self) -> dict[str, Any]:
            return {"strategies": [{"name": "alpha", "enabled": True}]}

        async def fetch_strategy(self, name: str) -> dict[str, Any]:
            return {
                "status": {"is_receiving_data": True},
                "performance": {"pnl": 10},
            }

    monkeypatch.setattr(websocket_module, "_STRATEGY_CLIENT", StubStrategyClient())

    class StubMarketClient:
        async def fetch_current_price(self, symbol: str) -> dict[str, Any]:
            return {"symbol": symbol, "price": 123.45}

    monkeypatch.setattr(websocket_module, "_MARKET_CLIENT", StubMarketClient())

    monkeypatch.setattr(websocket_module, "get_dom_service", lambda: None)
    monkeypatch.setattr(websocket_module, "get_dom_optimization_orchestrator", lambda: None)

    return app, account_client


def _drain_initial_messages(ws: WebSocketTestSession, count: int) -> list[dict[str, Any]]:
    return [ws.receive_json() for _ in range(count)]


def test_websocket_initial_and_orders_request(websocket_app: tuple[FastAPI, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    app, account_client = websocket_app

    async def fake_stats(_: str) -> dict[str, Any]:
        return {"stale": True}

    async def fake_compute(*_: Any, **__: Any) -> dict[str, Any]:
        raise RuntimeError("cache miss")

    monkeypatch.setattr(websocket_module, "cache_get_strategy_stats", fake_stats)
    monkeypatch.setattr(websocket_module, "cache_compute_performance", fake_compute)

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            messages = _drain_initial_messages(ws, 4)
            types = [msg["type"] for msg in messages]
            assert types == [
                "connection_status",
                "account_update",
                "orders_snapshot",
                "position_update",
            ]
            assert messages[0]["success"] is True
            assert messages[0].get("error_code") is None
            assert messages[1]["account_info"]["equity"] == 1000
            assert messages[2]["orders"][0]["order_id"] == "1"
            assert messages[3]["positions"][0]["symbol"] == "ES"

            ws.send_json({"type": "request_orders_snapshot", "limit": 5, "force": True})
            orders_payload = ws.receive_json()
            assert orders_payload["type"] == "orders_snapshot"
            assert orders_payload["success"] is True
            assert account_client.orders_calls[-1][:2] == (5, 0)

            ws.send_json({"type": "request_risk_rules", "force": True})
            risk_payload = ws.receive_json()
            assert risk_payload["type"] == "risk_rules"
            assert risk_payload["success"] is True
            assert isinstance(risk_payload["rules"], list)


def test_websocket_orders_service_unavailable(
    websocket_app: tuple[FastAPI, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, account_client = websocket_app

    async def failing_fetch_orders(
        *, limit: int, offset: int, start_date: str | None = None
    ) -> dict[str, Any]:
        account_client.orders_calls.append((limit, offset, start_date))
        raise ServiceUnavailableError("account", "offline")

    monkeypatch.setattr(account_client, "fetch_orders", failing_fetch_orders)

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            messages = _drain_initial_messages(ws, 4)
            types = [msg["type"] for msg in messages]
            assert types == [
                "connection_status",
                "account_update",
                "orders_snapshot",
                "position_update",
            ]

            orders_payload = messages[2]
            assert orders_payload["success"] is False
            assert (
                orders_payload["error"]
                == websocket_module.SERVICE_UNAVAILABLE_MESSAGE
            )
            assert orders_payload.get("meta", {}).get("limit") == 10

            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}


def test_websocket_strategy_requests(websocket_app: tuple[FastAPI, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = websocket_app

    async def stale_stats(_: str) -> dict[str, Any]:
        return {"stale": True}

    async def fail_compute(*_: Any, **__: Any) -> dict[str, Any]:
        raise RuntimeError("no cache")

    monkeypatch.setattr(websocket_module, "cache_get_strategy_stats", stale_stats)
    monkeypatch.setattr(websocket_module, "cache_compute_performance", fail_compute)

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            _drain_initial_messages(ws, 4)

            ws.send_json({"type": "request_strategy_status", "name": "alpha", "force": True})
            status_payload = ws.receive_json()
            assert status_payload["type"] == "strategy_status"
            assert status_payload["success"] is True
            assert status_payload["status"]["is_receiving_data"] is True

            ws.send_json({"type": "request_strategy_performance", "name": "alpha", "range": "1d", "force": True})
            performance_payload = ws.receive_json()
            assert performance_payload["type"] == "strategy_performance"
            assert performance_payload["success"] is True
            assert performance_payload["metrics"]["pnl"] == 10
