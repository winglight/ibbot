import importlib
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.routes import account as account_module
from src.routes import websocket as websocket_module
from src.account_snapshot_service import IB_COMMAND_SOURCE


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
from src.routes.service_clients import AccountServiceResult, ServiceUnavailableError


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
        "source": IB_COMMAND_SOURCE,
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
        "source": IB_COMMAND_SOURCE,
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

        async def fetch_info(
            self, *, force_refresh: bool = False
        ) -> AccountServiceResult:
            self.info_calls.append(force_refresh)
            return AccountServiceResult(
                data=account_snapshot["data"],
                metadata={"from_cache": False},
            )

        async def fetch_positions(
            self, *, fast: bool = True, force_refresh: bool = False
        ) -> AccountServiceResult:
            self.position_calls.append(force_refresh)
            return AccountServiceResult(
                data=list(positions_snapshot["data"]),
                metadata={"from_cache": False},
            )

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
    assert account_payload["source"] == IB_COMMAND_SOURCE
    assert account_payload["cache_source"] == IB_COMMAND_SOURCE
    assert account_payload["from_cache"] is False
    assert stub_client.info_calls == [True]

    assert positions_response.status_code == 200
    positions_payload = positions_response.json()
    assert positions_payload["stale"] is True
    assert positions_payload["snapshot_updated_at"] == positions_snapshot["updated_at"]
    assert positions_payload["source"] == IB_COMMAND_SOURCE
    assert positions_payload["cache_source"] == IB_COMMAND_SOURCE
    assert positions_payload["from_cache"] is False
    assert stub_client.position_calls == [True]


def test_account_info_uses_service_metadata(
    snapshot_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_read_account_snapshot() -> Optional[Dict[str, Any]]:
        return None

    async def fake_read_positions_snapshot() -> Optional[Dict[str, Any]]:
        return None

    metadata = {
        "source": "gateway",
        "snapshot_updated_at": "2024-06-02T09:30:00",
        "stale": False,
    }

    class StubClient:
        async def fetch_info(self, *, force_refresh: bool = False) -> AccountServiceResult:
            return AccountServiceResult(
                data={"equity": 2000.0, "connected": True},
                metadata=dict(metadata),
            )

        async def fetch_positions(self, *, fast: bool = True, force_refresh: bool = False) -> AccountServiceResult:
            return AccountServiceResult(data=[], metadata=dict(metadata))

    monkeypatch.setattr(
        account_module, "read_account_info_snapshot", fake_read_account_snapshot
    )
    monkeypatch.setattr(
        account_module, "read_positions_snapshot", fake_read_positions_snapshot
    )
    monkeypatch.setattr(account_module, "_get_account_service_client", lambda: StubClient())

    with TestClient(snapshot_app) as client:
        response = client.get("/api/account_info", params={"force_refresh": "true"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == metadata["source"]
    assert payload["snapshot_updated_at"] == metadata["snapshot_updated_at"]
    assert payload["from_cache"] is False
    assert payload["stale"] is False
    assert payload["account_info"]["equity"] == 2000.0


def test_account_info_returns_cached_when_service_unavailable(
    snapshot_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = {
        "data": {"equity": 1500.0, "connected": True},
        "updated_at": "2024-06-03T09:30:00",
        "source": IB_COMMAND_SOURCE,
    }

    async def fake_read_account_snapshot() -> Dict[str, Any]:
        return snapshot

    async def fake_read_positions_snapshot() -> Dict[str, Any]:
        return {
            "data": [],
            "updated_at": "2024-06-03T09:30:10",
            "source": IB_COMMAND_SOURCE,
        }

    class FailingClient:
        async def fetch_info(self, *, force_refresh: bool = False) -> AccountServiceResult:
            raise ServiceUnavailableError("account", "offline")

        async def fetch_positions(self, *, fast: bool = True, force_refresh: bool = False) -> AccountServiceResult:
            raise ServiceUnavailableError("account", "offline")

    monkeypatch.setattr(
        account_module, "read_account_info_snapshot", fake_read_account_snapshot
    )
    monkeypatch.setattr(
        account_module, "read_positions_snapshot", fake_read_positions_snapshot
    )
    monkeypatch.setattr(account_module, "_get_account_service_client", lambda: FailingClient())

    with TestClient(snapshot_app) as client:
        response = client.get("/api/account_info", params={"force_refresh": "true"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["from_cache"] is True
    assert payload["account_info"]["equity"] == 1500.0
    assert payload.get("warning") == "Account service unavailable"


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
            "source": IB_COMMAND_SOURCE,
            "stale": True,
        }

    async def fake_read_positions_snapshot() -> Dict[str, Any]:
        return {
            "data": [{"symbol": "ES", "position": 1}],
            "updated_at": "2024-06-01T10:00:05",
            "source": IB_COMMAND_SOURCE,
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
    monkeypatch.setattr(websocket_module, "get_dom_service_client", lambda: DummyDomClient())

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            status_messages: List[Dict[str, Any]] = []
            account_update: Optional[Dict[str, Any]] = None
            positions_update: Optional[Dict[str, Any]] = None
            for _ in range(10):
                message = websocket.receive_json()
                msg_type = message.get("type")
                if msg_type == "account_update" and account_update is None:
                    account_update = message
                elif msg_type == "position_update" and positions_update is None:
                    positions_update = message
                elif msg_type == "account_snapshot_status":
                    status_messages.append(message)
                if (
                    account_update is not None
                    and positions_update is not None
                    and len(status_messages) >= 2
                ):
                    break
            websocket.close()

    assert account_update is not None
    assert account_update["source"] == IB_COMMAND_SOURCE
    assert account_update["stale"] is True
    assert account_update.get("snapshot_updated_at") == "2024-06-01T10:00:00"

    assert positions_update is not None
    assert positions_update["source"] == IB_COMMAND_SOURCE
    assert positions_update["stale"] is True
    assert positions_update.get("snapshot_updated_at") == "2024-06-01T10:00:05"

    assert any(
        msg.get("snapshot") == "account_info" and msg.get("stale")
        for msg in status_messages
    )
    assert any(
        msg.get("snapshot") == "positions" and msg.get("stale")
        for msg in status_messages
    )


@pytest.mark.anyio("asyncio")
async def test_websocket_handshake_includes_service_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    app.include_router(websocket_module.router)

    metadata = {
        "source": "service",
        "snapshot_updated_at": "2024-06-05T09:30:00",
        "stale": False,
    }

    async def fake_load_health() -> Dict[str, Any]:
        return {"connected": True, "account_type": "INDIVIDUAL"}

    async def fake_fetch_account_info(*_, **__) -> AccountServiceResult:
        return AccountServiceResult(
            data={"equity": 2500.0, "connected": True}, metadata=dict(metadata)
        )

    async def fake_fetch_positions(*_, **__) -> AccountServiceResult:
        return AccountServiceResult(
            data=[{"symbol": "ES", "position": 1}], metadata=dict(metadata)
        )

    async def fake_fetch_orders(
        *, limit: int, offset: int, start_date: Optional[str] = None
    ) -> Optional[tuple[list[dict[str, Any]], Optional[int]]]:
        return [], 0

    async def no_account_snapshot() -> None:
        return None

    async def no_positions_snapshot() -> None:
        return None

    monkeypatch.setattr(websocket_module, "_services", lambda: None)
    monkeypatch.setattr(websocket_module, "_load_ib_command_health", fake_load_health)
    monkeypatch.setattr(websocket_module, "_fetch_account_info", fake_fetch_account_info)
    monkeypatch.setattr(websocket_module, "_fetch_positions", fake_fetch_positions)
    monkeypatch.setattr(websocket_module, "_fetch_orders", fake_fetch_orders)
    monkeypatch.setattr(websocket_module, "read_account_info_snapshot", no_account_snapshot)
    monkeypatch.setattr(websocket_module, "read_positions_snapshot", no_positions_snapshot)
    monkeypatch.setattr(websocket_module, "get_dom_service_client", lambda: DummyDomClient())

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            account_update: Optional[Dict[str, Any]] = None
            positions_update: Optional[Dict[str, Any]] = None
            for _ in range(8):
                message = websocket.receive_json()
                if message.get("type") == "account_update":
                    account_update = message
                elif message.get("type") == "position_update":
                    positions_update = message
                if account_update and positions_update:
                    break
            websocket.close()

    assert account_update is not None
    assert account_update["source"] == metadata["source"]
    assert account_update["snapshot_updated_at"] == metadata["snapshot_updated_at"]
    assert account_update["stale"] is metadata["stale"]
    assert account_update["account_info"]["equity"] == 2500.0

    assert positions_update is not None
    assert positions_update["source"] == metadata["source"]
    assert positions_update["snapshot_updated_at"] == metadata["snapshot_updated_at"]
    assert positions_update["stale"] is metadata["stale"]
