import importlib
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.routes import account as account_module
from src.routes import websocket as websocket_module
from src.routes.service_clients import ServiceUnavailableError


@pytest.fixture()
def snapshot_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    app = FastAPI()
    app.include_router(account_module.router)

    async def allow_user(request: Request) -> str:
        request.state.is_read_only = False
        request.state.user_role = "admin"
        request.state.username = "tester"
        return "tester"

    app.dependency_overrides[account_module.get_current_user] = allow_user
    return app


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_main_module_does_not_expose_snapshot_service() -> None:
    module = importlib.import_module("main")
    assert not hasattr(module, "AccountSnapshotService")


def test_account_info_and_positions_include_snapshot_status(
    snapshot_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    account_snapshot = {
        "data": {"equity": 123.45, "connected": True},
        "updated_at": "2024-06-01T09:30:00",
        "source": "redis",
        "stale": True,
        "stale_reason": "refreshing",
        "stale_at": "2024-06-01T09:31:00",
    }
    positions_snapshot = {
        "data": [
            {"symbol": "ES", "position": 1},
            {"symbol": "NQ", "position": -2},
        ],
        "updated_at": "2024-06-01T09:32:00",
        "source": "redis",
        "stale": True,
    }

    async def fake_read_account_snapshot() -> Dict[str, Any]:
        return account_snapshot

    async def fake_read_positions_snapshot() -> Dict[str, Any]:
        return positions_snapshot

    class StubClient:
        def __init__(self) -> None:
            self.info_calls: List[bool] = []
            self.position_calls: List[bool] = []

        async def fetch_info(self, *, force_refresh: bool = False) -> Any:
            self.info_calls.append(force_refresh)
            raise AssertionError("fetch_info should not be called when cache is used")

        async def fetch_positions(
            self, *, fast: bool = True, force_refresh: bool = False
        ) -> Any:
            self.position_calls.append(force_refresh)
            raise AssertionError("fetch_positions should not be called when cache is used")

    stub_client = StubClient()

    monkeypatch.setattr(
        account_module, "read_account_info_snapshot", fake_read_account_snapshot
    )
    monkeypatch.setattr(
        account_module, "read_positions_snapshot", fake_read_positions_snapshot
    )
    monkeypatch.setattr(
        account_module, "_get_account_service_client", lambda: stub_client
    )

    with TestClient(snapshot_app) as client:
        account_response = client.get("/api/account_info")
        positions_response = client.get("/api/positions")

    assert account_response.status_code == 200
    account_payload = account_response.json()
    assert account_payload["stale"] is True
    assert account_payload["snapshot_updated_at"] == account_snapshot["updated_at"]
    assert account_payload["stale_reason"] == account_snapshot["stale_reason"]
    assert account_payload["stale_at"] == account_snapshot["stale_at"]
    assert account_payload["source"] == account_snapshot["source"]

    assert positions_response.status_code == 200
    positions_payload = positions_response.json()
    assert positions_payload["stale"] is True
    assert positions_payload["snapshot_updated_at"] == positions_snapshot["updated_at"]
    assert positions_payload["source"] == positions_snapshot["source"]


def test_account_endpoints_fallback_to_cache_when_service_unavailable(
    snapshot_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    account_snapshot = {
        "data": {"equity": 999.0},
        "updated_at": "2024-06-02T09:30:00",
        "source": "redis",
        "stale": False,
    }
    positions_snapshot = {
        "data": [{"symbol": "ES", "position": 3}],
        "updated_at": "2024-06-02T09:31:00",
        "source": "redis",
        "stale": False,
    }

    async def fake_read_account_snapshot() -> Dict[str, Any]:
        return account_snapshot

    async def fake_read_positions_snapshot() -> Dict[str, Any]:
        return positions_snapshot

    class FailingClient:
        async def fetch_info(self, *, force_refresh: bool = False) -> Any:
            raise ServiceUnavailableError("account", "offline")

        async def fetch_positions(
            self, *, fast: bool = True, force_refresh: bool = False
        ) -> Any:
            raise ServiceUnavailableError("account", "offline")

    monkeypatch.setattr(
        account_module, "read_account_info_snapshot", fake_read_account_snapshot
    )
    monkeypatch.setattr(
        account_module, "read_positions_snapshot", fake_read_positions_snapshot
    )
    monkeypatch.setattr(account_module, "_get_account_service_client", lambda: FailingClient())

    with TestClient(snapshot_app) as client:
        info_response = client.get("/api/account_info", params={"force_refresh": "true"})
        positions_response = client.get(
            "/api/positions", params={"force_refresh": "true", "fast": "true"}
        )

    assert info_response.status_code == 200
    info_payload = info_response.json()
    assert info_payload["success"] is True
    assert info_payload["from_cache"] is True
    assert info_payload["account_info"] == account_snapshot["data"]
    assert info_payload.get("warning") == "Account service unavailable"
    assert info_payload["source"] == account_snapshot["source"]
    assert info_payload["snapshot_updated_at"] == account_snapshot["updated_at"]

    assert positions_response.status_code == 200
    positions_payload = positions_response.json()
    assert positions_payload["success"] is True
    assert positions_payload["from_cache"] is True
    assert positions_payload["positions"] == positions_snapshot["data"]
    assert positions_payload.get("warning") == "Account service unavailable"
    assert positions_payload["source"] == positions_snapshot["source"]
    assert positions_payload["snapshot_updated_at"] == positions_snapshot["updated_at"]


@pytest.mark.anyio("asyncio")
async def test_websocket_emits_snapshot_status_when_cache_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    app.include_router(websocket_module.router)

    class DummyWebSocketManager:
        async def connect(self, websocket) -> None:
            await websocket.accept()

        async def broadcast(self, payload: Any) -> None:
            return None

        def disconnect(self, websocket) -> None:
            return None

    class DummyServices:
        def __init__(self) -> None:
            self.websocket_manager = DummyWebSocketManager()

    dummy_services = DummyServices()

    async def fake_load_health() -> Dict[str, Any]:
        return {"connected": True, "account_type": "INDIVIDUAL"}

    async def fake_fetch_account_info(*_, **__) -> None:
        return None

    async def fake_fetch_positions(*_, **__) -> None:
        return None

    async def fake_fetch_orders(
        *, limit: int, offset: int, start_date: Optional[str] = None
    ) -> Optional[tuple[list[dict[str, Any]], Optional[int]]]:
        return [], 0

    async def fake_read_account_snapshot() -> Dict[str, Any]:
        return {
            "data": {"equity": 1000.0},
            "updated_at": "2024-06-01T10:00:00",
            "source": "redis",
            "stale": True,
        }

    async def fake_read_positions_snapshot() -> Dict[str, Any]:
        return {
            "data": [{"symbol": "ES", "position": 1}],
            "updated_at": "2024-06-01T10:00:05",
            "source": "redis",
            "stale": True,
        }

    monkeypatch.setattr(websocket_module, "_services", lambda: dummy_services)
    monkeypatch.setattr(websocket_module, "_load_ib_command_health", fake_load_health)
    monkeypatch.setattr(websocket_module, "_fetch_account_info", fake_fetch_account_info)
    monkeypatch.setattr(websocket_module, "_fetch_positions", fake_fetch_positions)
    monkeypatch.setattr(websocket_module, "_fetch_orders", fake_fetch_orders)
    monkeypatch.setattr(
        websocket_module, "read_account_info_snapshot", fake_read_account_snapshot
    )
    monkeypatch.setattr(
        websocket_module, "read_positions_snapshot", fake_read_positions_snapshot
    )
    monkeypatch.setattr(websocket_module, "get_dom_service", lambda: None)

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            status_messages: List[Dict[str, Any]] = []
            for _ in range(6):
                message = websocket.receive_json()
                if message.get("type") == "account_snapshot_status":
                    status_messages.append(message)
                if len(status_messages) >= 2:
                    break
            websocket.close()

    assert any(
        msg.get("snapshot") == "account_info" and msg.get("stale")
        for msg in status_messages
    )
    assert any(
        msg.get("snapshot") == "positions" and msg.get("stale")
        for msg in status_messages
    )


@pytest.mark.anyio("asyncio")
async def test_websocket_initial_payload_uses_cache_when_service_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    app.include_router(websocket_module.router)

    class DummyWebSocketManager:
        async def connect(self, websocket) -> None:
            await websocket.accept()

        async def broadcast(self, payload: Any) -> None:
            return None

        def disconnect(self, websocket) -> None:
            return None

    class DummyServices:
        def __init__(self) -> None:
            self.websocket_manager = DummyWebSocketManager()

    dummy_services = DummyServices()

    async def fake_health() -> Dict[str, Any]:
        return {"connected": True, "account_type": "INDIVIDUAL"}

    async def fake_fetch_account_info(*_, **__) -> None:
        return None

    async def fake_fetch_positions(*_, **__) -> None:
        return None

    async def fake_fetch_orders(
        *, limit: int, offset: int, start_date: Optional[str] = None
    ) -> Optional[tuple[list[dict[str, Any]], Optional[int]]]:
        return [], 0

    async def fake_read_account_snapshot() -> Dict[str, Any]:
        return {
            "data": {"equity": 1000.0},
            "updated_at": "2024-06-02T10:00:00",
            "source": "redis",
            "stale": False,
        }

    async def fake_read_positions_snapshot() -> Dict[str, Any]:
        return {
            "data": [{"symbol": "MES", "position": 2}],
            "updated_at": "2024-06-02T10:00:05",
            "source": "redis",
            "stale": False,
        }

    monkeypatch.setattr(websocket_module, "_services", lambda: dummy_services)
    monkeypatch.setattr(websocket_module, "_load_ib_command_health", fake_health)
    monkeypatch.setattr(websocket_module, "_fetch_account_info", fake_fetch_account_info)
    monkeypatch.setattr(websocket_module, "_fetch_positions", fake_fetch_positions)
    monkeypatch.setattr(websocket_module, "_fetch_orders", fake_fetch_orders)
    monkeypatch.setattr(
        websocket_module, "read_account_info_snapshot", fake_read_account_snapshot
    )
    monkeypatch.setattr(
        websocket_module, "read_positions_snapshot", fake_read_positions_snapshot
    )
    monkeypatch.setattr(websocket_module, "get_dom_service", lambda: None)

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            account_message: Optional[Dict[str, Any]] = None
            positions_message: Optional[Dict[str, Any]] = None
            for _ in range(6):
                message = websocket.receive_json()
                if message.get("type") == "account_update":
                    account_message = message
                elif message.get("type") == "position_update":
                    positions_message = message
                if account_message and positions_message:
                    break
            websocket.close()

    assert account_message is not None
    assert account_message.get("source") == "redis"
    assert account_message.get("snapshot_updated_at") == "2024-06-02T10:00:00"
    assert account_message.get("success") is True
    account_info = account_message.get("account_info")
    assert isinstance(account_info, dict)
    assert account_info.get("equity") == 1000.0

    assert positions_message is not None
    assert positions_message.get("source") == "redis"
    assert positions_message.get("snapshot_updated_at") == "2024-06-02T10:00:05"
    assert positions_message.get("success") is True
    assert positions_message.get("positions") == [{"symbol": "MES", "position": 2}]
