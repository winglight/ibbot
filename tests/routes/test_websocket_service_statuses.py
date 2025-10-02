from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.account_service_readiness import AccountServiceReadiness
from src.routes import websocket as websocket_module
import src.service_status as service_status_module


class StubRedis:
    def __init__(self, mapping: Dict[str, Dict[str, str] | None]) -> None:
        self._mapping = mapping

    async def hgetall(self, key: str):  # type: ignore[override]
        value = self._mapping.get(key)
        if value is None:
            return {}
        return dict(value)


class DummyWebSocketManager:
    async def connect(self, websocket) -> None:
        await websocket.accept()

    async def broadcast(self, payload):  # pragma: no cover - not used
        return None

    def disconnect(self, websocket) -> None:
        return None


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio("asyncio")
async def test_websocket_service_statuses_include_account_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    app.include_router(websocket_module.router)

    manager = DummyWebSocketManager()
    monkeypatch.setattr(websocket_module, "_websocket_manager", lambda: manager)

    async def fake_health() -> Dict[str, object]:
        return {
            "connected": True,
            "account_type": "INDIVIDUAL",
            "stale": False,
            "ib_connected": True,
        }

    async def empty_snapshot(*_: object, **__: object) -> Dict[str, Any]:
        return {}

    async def noop_fetch_orders(*_: object, **__: object) -> tuple[list[Any], int]:
        return [], 0

    monkeypatch.setattr(websocket_module, "_load_ib_command_health", fake_health)
    monkeypatch.setattr(websocket_module, "read_account_info_snapshot", empty_snapshot)
    monkeypatch.setattr(websocket_module, "read_positions_snapshot", empty_snapshot)
    monkeypatch.setattr(websocket_module, "_fetch_account_info", empty_snapshot)
    monkeypatch.setattr(websocket_module, "_fetch_positions", empty_snapshot)
    monkeypatch.setattr(websocket_module, "_fetch_orders", noop_fetch_orders)
    monkeypatch.setattr(websocket_module, "get_dom_service", lambda: None)

    now = datetime.now(timezone.utc).isoformat()
    redis_data: Dict[str, Dict[str, str] | None] = {
        "hash:service:health:ib-command": {
            "last_seen": now,
            "queue_depth": "0",
        },
        "hash:service:health:account-service": {
            "status": "running",
            "last_seen": now,
            "snapshot_updated_at": now,
            "stale": "false",
            "stale_reason": "",
        },
        "hash:service:queue-monitor": {"timestamp": now},
    }

    stub_redis = StubRedis(redis_data)

    class Container:
        redis_client = stub_redis

    monkeypatch.setattr(websocket_module.core, "get_container", lambda: Container())

    async def fake_readiness(**_: object) -> AccountServiceReadiness:
        return AccountServiceReadiness(ready=True, checked_at=datetime.now(timezone.utc), error=None)

    monkeypatch.setattr(service_status_module, "refresh_account_service_status", fake_readiness)

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            initial = websocket.receive_json()
            assert initial["type"] == "connection_status"

            websocket.send_json({"type": "request_service_statuses", "force": True})
            while True:
                message = websocket.receive_json()
                if message.get("type") == "service_statuses":
                    break

            assert message.get("success") is True
            services = message.get("services") or []
            account_entry = next(
                (entry for entry in services if entry.get("service") == "account"),
                None,
            )
            assert account_entry is not None
            account_metrics = (account_entry.get("payload") or {}).get("metrics") or {}
            assert account_metrics.get("snapshot_updated_at") == now
            assert account_metrics.get("stale") is False
            assert "stale_reason" in account_metrics

            websocket.close()
