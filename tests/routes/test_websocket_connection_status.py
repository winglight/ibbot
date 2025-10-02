from typing import Dict

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.routes import websocket as websocket_module


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
        return {"connected": True, "account_type": "INDIVIDUAL", "stale": False}

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
    monkeypatch.setattr(websocket_module, "get_dom_service", lambda: None)
    monkeypatch.setattr(websocket_module, "get_dom_optimization_orchestrator", lambda: None)

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
