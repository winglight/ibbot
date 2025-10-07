import asyncio
import json
from typing import Any, Dict

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from src.routes import websocket as websocket_module


class DummyDomClient:
    async def get_stats(
        self,
        symbol: str,
        *,
        lookback_seconds: int | None = None,
        start: str | None = None,
        end: str | None = None,
        max_points: int | None = None,
    ) -> Dict[str, Any]:
        return {"success": True, "symbol": symbol, "stats": {}}

    async def get_history(
        self,
        symbol: str,
        *,
        limit: int | None = None,
        start: str | None = None,
        end: str | None = None,
        include_snapshots: bool | None = None,
    ) -> Dict[str, Any]:
        return {"success": True, "symbol": symbol, "history": []}

    async def get_data(self, symbol: str | None = None) -> Dict[str, Any]:
        return {"success": True, "metrics": {}, "depth": {}}

    async def get_alerts(self) -> Dict[str, Any]:
        return {"success": True, "alerts": []}

    async def acknowledge_alert(self, alert_id: int) -> Dict[str, Any]:
        return {"success": True}

    async def start_dom(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"success": True}

    async def stop_dom(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"success": True}

    async def update_config(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"success": True}
from src.routes.service_clients import ServiceUnavailableError


@pytest.fixture()
def websocket_app(monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, Any]:
    app = FastAPI()
    app.include_router(websocket_module.router)

    class StubWebSocketManager:
        def __init__(self) -> None:
            self.connections: list[Any] = []
            self.broadcasts: list[dict[str, Any]] = []

        async def connect(self, websocket) -> None:
            await websocket.accept()
            self.connections.append(websocket)

        def disconnect(self, websocket) -> None:
            if websocket in self.connections:
                self.connections.remove(websocket)

        async def broadcast(self, message: dict[str, Any]) -> None:
            self.broadcasts.append(dict(message))
            text = json.dumps(message)
            for connection in list(self.connections):
                await connection.send_text(text)

    stub_manager = StubWebSocketManager()
    monkeypatch.setattr(websocket_module, "_websocket_manager", lambda: stub_manager)

    async def noop_risk_listener() -> None:
        return None

    monkeypatch.setattr(
        websocket_module,
        "_ensure_risk_rules_listener",
        noop_risk_listener,
    )

    async def fake_health() -> dict[str, Any]:
        return {
            "connected": True,
            "account_type": "Paper",
            "error_code": None,
            "stale": False,
            "ib_connected": True,
            "ib_connection_error": "",
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

        async def fetch_info(
            self,
            *,
            force_refresh: bool = False,
            fast: bool = False,
        ) -> dict[str, Any]:
            return {"equity": 1000, "currency": "USD"}

        async def fetch_positions(
            self, *, force_refresh: bool = False, fast: bool = True
        ) -> list[dict[str, Any]]:
            return [{"symbol": "ES", "position": 1}]

    account_client = StubAccountClient()
    monkeypatch.setattr(websocket_module, "_ACCOUNT_CLIENT", account_client)

    class StubOrdersClient:
        def __init__(self) -> None:
            self.default_start_date = "2024-05-10T00:00:00+00:00"
            self.default_start_date_timezone = "UTC"

        async def list_orders(
            self,
            *,
            limit: int,
            offset: int,
            start_date: str | None = None,
        ) -> dict[str, Any]:
            account_client.orders_calls.append((limit, offset, start_date))
            payload: dict[str, Any] = {
                "orders": [{"order_id": "1", "symbol": "ES", "notes": "Review setup"}],
                "total": 1,
            }
            if start_date:
                payload["start_date"] = start_date
                payload["start_date_timezone"] = self.default_start_date_timezone
            elif self.default_start_date:
                payload["start_date"] = self.default_start_date
                payload["start_date_timezone"] = self.default_start_date_timezone
            return payload

    orders_client = StubOrdersClient()
    monkeypatch.setattr(websocket_module, "_ORDERS_CLIENT", orders_client)

    class StubRiskClient:
        async def fetch_rules(self) -> dict[str, Any]:
            return {
                "rules": {
                    "ES": {
                        "symbol": "ES",
                        "enabled": True,
                        "metadata": {
                            "rule_type": "trailing",
                            "atr_params": {
                                "multiplier_sl": 1.6,
                                "multiplier_tp": 2.4,
                            },
                        },
                    }
                }
            }

    monkeypatch.setattr(websocket_module, "_RISK_CLIENT", StubRiskClient())

    class StubStrategyClient:
        async def list_strategies(self) -> dict[str, Any]:
            return {"strategies": [{"name": "alpha", "enabled": True}]}

        async def get_strategy_status(self, name: str) -> dict[str, Any]:
            return {
                "status": {
                    "name": name,
                    "state": "idle",
                    "is_receiving_data": True,
                }
            }

        async def fetch_strategy(self, name: str) -> dict[str, Any]:
            return {
                "status": {"is_receiving_data": True},
                "performance": {"pnl": 10},
            }

        async def get_strategy_performance(
            self,
            name: str,
            *,
            range_: str | None = None,
            start: str | None = None,
            end: str | None = None,
        ) -> dict[str, Any]:
            return {
                "metrics": {
                    "pnl": 10,
                    "range": range_ or "",
                },
                "performance": {
                    "name": name,
                    "range": range_ or "",
                    "points": [],
                }
            }

    monkeypatch.setattr(websocket_module, "_STRATEGY_CLIENT", StubStrategyClient())

    class StubMarketClient:
        async def fetch_current_price(self, symbol: str) -> dict[str, Any]:
            return {"symbol": symbol, "price": 123.45}

    monkeypatch.setattr(websocket_module, "_MARKET_CLIENT", StubMarketClient())

    if not hasattr(websocket_module, "cache_get_strategy_stats"):
        monkeypatch.setattr(
            websocket_module,
            "cache_get_strategy_stats",
            lambda name: {},
            raising=False,
        )
    if not hasattr(websocket_module, "cache_compute_performance"):
        async def _noop_cache_compute(*_: Any, **__: Any) -> dict[str, Any]:
            return {}

        monkeypatch.setattr(
            websocket_module,
            "cache_compute_performance",
            _noop_cache_compute,
            raising=False,
        )

    monkeypatch.setattr(websocket_module, "get_dom_service_client", lambda: DummyDomClient())

    return app, account_client


def _drain_initial_messages(ws: WebSocketTestSession, count: int) -> list[dict[str, Any]]:
    return [ws.receive_json() for _ in range(count)]


def _receive_until_type(
    ws: WebSocketTestSession, expected_type: str, *, max_messages: int = 5
) -> dict[str, Any]:
    for _ in range(max_messages):
        payload = ws.receive_json()
        if payload.get("type") == expected_type:
            return payload
    raise AssertionError(f"Did not receive payload of type {expected_type!r}")


def test_websocket_initial_and_orders_request(websocket_app: tuple[FastAPI, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    app, account_client = websocket_app
    orders_client = websocket_module._ORDERS_CLIENT

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
            assert messages[2]["orders"][0]["notes"] == "Review setup"
            assert messages[2]["meta"]["start_date"] == orders_client.default_start_date
            assert (
                messages[2]["meta"].get("start_date_timezone")
                == orders_client.default_start_date_timezone
            )
            assert messages[3]["positions"][0]["symbol"] == "ES"

            ws.send_json({"type": "request_orders_snapshot", "limit": 5, "force": True})
            orders_payload = _receive_until_type(ws, "orders_snapshot")
            assert orders_payload["type"] == "orders_snapshot"
            assert orders_payload["success"] is True
            assert account_client.orders_calls[-1][:2] == (5, 0)
            assert orders_payload["orders"][0]["notes"] == "Review setup"
            assert orders_payload["meta"]["start_date"] == orders_client.default_start_date
            assert (
                orders_payload["meta"].get("start_date_timezone")
                == orders_client.default_start_date_timezone
            )

            ws.send_json({"type": "request_risk_rules", "force": True})
            risk_payload = _receive_until_type(ws, "risk_rules")
            assert risk_payload["type"] == "risk_rules"
            assert risk_payload["success"] is True
            assert isinstance(risk_payload["rules"], dict)
            es_rule = risk_payload["rules"]["ES"]
            assert es_rule["rule_type"] == "trailing"
            assert es_rule["atr_params"] == {
                "multiplier_sl": 1.6,
                "multiplier_tp": 2.4,
            }
            assert "rule_type" not in (es_rule.get("metadata") or {})


def test_websocket_orders_service_unavailable(
    websocket_app: tuple[FastAPI, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, account_client = websocket_app

    orders_client = websocket_module._ORDERS_CLIENT

    async def failing_fetch_orders(
        *, limit: int, offset: int, start_date: str | None = None
    ) -> dict[str, Any]:
        account_client.orders_calls.append((limit, offset, start_date))
        raise ServiceUnavailableError("orders", "offline")

    monkeypatch.setattr(orders_client, "list_orders", failing_fetch_orders)

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
            assert _receive_until_type(ws, "pong") == {"type": "pong"}


def test_websocket_risk_service_unavailable(
    websocket_app: tuple[FastAPI, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = websocket_app

    risk_client = websocket_module._RISK_CLIENT

    async def failing_fetch_rules() -> dict[str, Any]:
        raise ServiceUnavailableError("risk", "offline")

    monkeypatch.setattr(risk_client, "fetch_rules", failing_fetch_rules)

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            _drain_initial_messages(ws, 4)
            ws.send_json({"type": "request_risk_rules", "force": True})
            payload = _receive_until_type(ws, "risk_rules")
            assert payload["type"] == "risk_rules"
            assert payload["success"] is False
            assert payload["error"] == websocket_module.SERVICE_UNAVAILABLE_MESSAGE


def test_websocket_strategy_status_service_unavailable(
    websocket_app: tuple[FastAPI, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = websocket_app

    class FailingStrategyClient:
        async def list_strategies(self) -> dict[str, Any]:
            return {"strategies": []}

        async def get_strategy_status(self, name: str) -> dict[str, Any]:
            raise ServiceUnavailableError("strategy", "offline")

    monkeypatch.setattr(websocket_module, "_STRATEGY_CLIENT", FailingStrategyClient())

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            _drain_initial_messages(ws, 4)
            ws.send_json(
                {
                    "type": "request_strategy_status",
                    "name": "alpha",
                    "force": True,
                }
            )
            payload = _receive_until_type(ws, "strategy_status")
            assert payload["type"] == "strategy_status"
            assert payload["success"] is False
            assert payload["error"] == websocket_module.SERVICE_UNAVAILABLE_MESSAGE
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
            status_payload = _receive_until_type(ws, "strategy_status")
            assert status_payload["type"] == "strategy_status"
            assert status_payload["success"] is True
            assert status_payload["status"]["is_receiving_data"] is True

            ws.send_json({"type": "request_strategy_performance", "name": "alpha", "range": "1d", "force": True})
            performance_payload = _receive_until_type(ws, "strategy_performance")
            assert performance_payload["type"] == "strategy_performance"
            assert performance_payload["success"] is True
            assert performance_payload["metrics"]["pnl"] == 10


def test_websocket_broadcasts_risk_rules_updates(
    websocket_app: tuple[FastAPI, Any],
) -> None:
    app, _ = websocket_app
    stub_manager = websocket_module._websocket_manager()
    stub_manager.broadcasts.clear()

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            _drain_initial_messages(ws, 4)
            payload = {
                "rules": {
                    "CL": {
                        "symbol": "CL",
                        "enabled": True,
                        "metadata": {
                            "rule_type": "trailing",
                            "atr_params": {"multiplier_sl": 1.2},
                        },
                    }
                },
                "updated_at": "2024-01-01T00:00:00+00:00",
                "source": "test",
            }
            asyncio.run(
                websocket_module._handle_risk_rules_pubsub(
                    websocket_module.RISK_RULES_UPDATE_CHANNEL,
                    json.dumps(payload),
                )
            )
            message = _receive_until_type(ws, "risk_rules")
            assert message["rules"]["CL"]["rule_type"] == "trailing"
            assert message["rules"]["CL"]["atr_params"]["multiplier_sl"] == 1.2
            assert "rule_type" not in (message["rules"]["CL"].get("metadata") or {})
            broadcast_rule = stub_manager.broadcasts[-1]["rules"]["CL"]
            assert broadcast_rule["rule_type"] == "trailing"
