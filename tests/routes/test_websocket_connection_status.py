from typing import Dict

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio("asyncio")
async def test_websocket_emits_connection_status(monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastAPI()
    app.include_router(websocket_module.router)

    class DummyWebSocketManager:
        async def connect(self, websocket) -> None:
            await websocket.accept()

        async def broadcast(self, payload):  # pragma: no cover - not used
            return None

        def disconnect(self, websocket) -> None:
            return None

    manager = DummyWebSocketManager()
    monkeypatch.setattr(websocket_module, "_websocket_manager", lambda: manager)

    async def fake_health() -> Dict[str, object]:
        return {
            "connected": True,
            "account_type": "INDIVIDUAL",
            "stale": False,
            "ib_connected": True,
            "ib_connection_error": "",
        }

    async def skip_snapshot(*args, **kwargs):  # pragma: no cover - expected path
        raise RuntimeError("skip snapshot work")

    async def noop_fetch_account(*args, **kwargs):  # pragma: no cover - unused
        return None

    async def noop_fetch_positions(*args, **kwargs):  # pragma: no cover - unused
        return None

    async def stub_fetch_orders(*args, **kwargs):  # pragma: no cover - unused
        return [], 0

    monkeypatch.setattr(websocket_module, "_load_ib_command_health", fake_health)
    monkeypatch.setattr(websocket_module, "read_account_info_snapshot", skip_snapshot)
    monkeypatch.setattr(websocket_module, "read_positions_snapshot", skip_snapshot)
    monkeypatch.setattr(websocket_module, "_fetch_account_info", noop_fetch_account)
    monkeypatch.setattr(websocket_module, "_fetch_positions", noop_fetch_positions)
    monkeypatch.setattr(websocket_module, "_fetch_orders", stub_fetch_orders)
    monkeypatch.setattr(websocket_module, "get_dom_service_client", lambda: DummyDomClient())

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            message = websocket.receive_json()
            assert message["type"] == "connection_status"
            assert message["success"] is True
            assert message["connected"] is True
            assert message["account_type"] == "INDIVIDUAL"

            health = message.get("health")
            assert isinstance(health, dict)
            assert health.get("connected") is True
            assert health.get("account_type") == "INDIVIDUAL"
            assert health.get("stale") is False

            assert "error" not in message
            websocket.close()


@pytest.mark.anyio("asyncio")
async def test_websocket_emits_disconnected_status(monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastAPI()
    app.include_router(websocket_module.router)

    class DummyWebSocketManager:
        async def connect(self, websocket) -> None:
            await websocket.accept()

        async def broadcast(self, payload):  # pragma: no cover - not used
            return None

        def disconnect(self, websocket) -> None:
            return None

    manager = DummyWebSocketManager()
    monkeypatch.setattr(websocket_module, "_websocket_manager", lambda: manager)

    async def fake_health() -> Dict[str, object]:
        return {
            "connected": False,
            "account_type": "INDIVIDUAL",
            "stale": False,
            "last_error": "gateway down",
            "ib_connected": False,
            "ib_connection_error": "gateway down",
        }

    async def empty_account_snapshot(*_, **__):
        return []

    async def empty_positions_snapshot(*_, **__):
        return []

    async def empty_account_info(*_, **__):
        return {}

    async def empty_positions(*_, **__):
        return [], 0

    async def empty_orders(*_, **__):
        return [], 0

    monkeypatch.setattr(websocket_module, "_load_ib_command_health", fake_health)
    monkeypatch.setattr(websocket_module, "read_account_info_snapshot", empty_account_snapshot)
    monkeypatch.setattr(websocket_module, "read_positions_snapshot", empty_positions_snapshot)
    monkeypatch.setattr(websocket_module, "_fetch_account_info", empty_account_info)
    monkeypatch.setattr(websocket_module, "_fetch_positions", empty_positions)
    monkeypatch.setattr(websocket_module, "_fetch_orders", empty_orders)
    monkeypatch.setattr(websocket_module, "get_dom_service_client", lambda: DummyDomClient())

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            message = websocket.receive_json()
            assert message["type"] == "connection_status"
            assert message["success"] is True
            assert message["connected"] is False
            assert message.get("last_error") == "gateway down"

            health = message.get("health")
            assert isinstance(health, dict)
            assert health.get("connected") is False
            assert health.get("last_error") == "gateway down"


@pytest.mark.anyio("asyncio")
async def test_websocket_handles_service_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastAPI()
    app.include_router(websocket_module.router)

    class DummyWebSocketManager:
        async def connect(self, websocket) -> None:
            await websocket.accept()

        async def broadcast(self, payload):  # pragma: no cover - not used
            return None

        def disconnect(self, websocket) -> None:
            return None

    manager = DummyWebSocketManager()
    monkeypatch.setattr(websocket_module, "_websocket_manager", lambda: manager)

    async def fake_health() -> Dict[str, object]:
        return {
            "connected": True,
            "account_type": "INDIVIDUAL",
            "stale": False,
        }

    async def empty_snapshot(*_, **__):
        return {}

    async def fail_fetch_info(*_, **__):
        raise websocket_module.ServiceUnavailableError(
            "account-service", "account offline"
        )

    async def fail_fetch_positions(*_, **__):
        raise websocket_module.ServiceUnavailableError(
            "account-service", "positions offline"
        )

    async def fail_list_orders(*_, **__):
        raise websocket_module.ServiceUnavailableError(
            "orders-service", "orders offline"
        )

    monkeypatch.setattr(websocket_module, "_load_ib_command_health", fake_health)
    monkeypatch.setattr(websocket_module, "read_account_info_snapshot", empty_snapshot)
    monkeypatch.setattr(websocket_module, "read_positions_snapshot", empty_snapshot)
    monkeypatch.setattr(websocket_module._ACCOUNT_CLIENT, "fetch_info", fail_fetch_info)
    monkeypatch.setattr(
        websocket_module._ACCOUNT_CLIENT, "fetch_positions", fail_fetch_positions
    )
    monkeypatch.setattr(websocket_module._ORDERS_CLIENT, "list_orders", fail_list_orders)
    monkeypatch.setattr(websocket_module, "get_dom_service_client", lambda: DummyDomClient())

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            connection_status = websocket.receive_json()
            assert connection_status["type"] == "connection_status"
            assert connection_status["success"] is True

            account_message = websocket.receive_json()
            assert account_message["type"] == "account_update"
            assert account_message["success"] is False
            assert account_message["error"] == websocket_module.SERVICE_UNAVAILABLE_MESSAGE
            account_info = account_message.get("account_info")
            assert isinstance(account_info, dict)
            assert account_info.get("connected") is True

            orders_message = websocket.receive_json()
            assert orders_message["type"] == "orders_snapshot"
            assert orders_message["success"] is False
            assert orders_message["error"] == websocket_module.SERVICE_UNAVAILABLE_MESSAGE

            positions_message = websocket.receive_json()
            assert positions_message["type"] == "position_update"
            assert positions_message["success"] is False
            assert positions_message["error"] == websocket_module.SERVICE_UNAVAILABLE_MESSAGE
            assert positions_message.get("positions") == []
            assert positions_message.get("details")

            websocket.send_json({"type": "ping"})
            pong = websocket.receive_json()
            assert pong == {"type": "pong"}
