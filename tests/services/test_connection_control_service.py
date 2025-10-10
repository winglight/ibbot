from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.connection_control import main as service_module


class DummyRedis:
    async def close(self) -> None:  # pragma: no cover - compatibility shim
        return None


@pytest.fixture(autouse=True)
def _patch_dependencies(monkeypatch: pytest.MonkeyPatch):
    async def fake_get_redis_client(*args: Any, **kwargs: Any) -> DummyRedis:
        return DummyRedis()

    async def fake_close_global_client() -> None:
        return None

    async def fake_update_service_health(*args: Any, **kwargs: Any) -> None:
        return None

    async def fake_get_queue_length(*args: Any, **kwargs: Any) -> int:
        return 0

    monkeypatch.setattr(service_module, "get_redis_client", fake_get_redis_client)
    monkeypatch.setattr(service_module, "close_global_client", fake_close_global_client)
    monkeypatch.setattr(service_module, "update_service_health", fake_update_service_health)
    monkeypatch.setattr(service_module, "get_queue_length", fake_get_queue_length)

    commands: list[dict[str, Any]] = []

    async def fake_enqueue(command: dict[str, Any], *, redis: Any = None, queue_key: str | None = None) -> None:
        commands.append(dict(command))

    monkeypatch.setattr(service_module, "enqueue_ib_command", fake_enqueue)

    monkeypatch.setenv("IBKR_HOST", "env-host")
    monkeypatch.setenv("IBKR_PORT", "4102")
    monkeypatch.setenv("IBKR_CLIENT_ID", "12")
    monkeypatch.setenv("IBKR_CLIENT_ID_MIN", "10")
    monkeypatch.setenv("IBKR_CLIENT_ID_MAX", "19")
    monkeypatch.setenv("IBKR_PAPER_TRADING", "false")

    return {"commands": commands}


def test_connection_config_uses_environment(_patch_dependencies) -> None:
    app = service_module.create_app(health_interval=0)

    with TestClient(app) as client:
        response = client.get("/connection/config")

    assert response.status_code == 200
    assert response.json() == {
        "host": "env-host",
        "port": 4102,
        "client_id": 12,
        "paper_trading": False,
    }


def test_connect_endpoint_uses_env_and_enqueues(_patch_dependencies) -> None:
    commands: list[dict[str, Any]] = _patch_dependencies["commands"]

    app = service_module.create_app(health_interval=0)

    payload = {
        "host": "1.2.3.4",
        "port": 4001,
        "client_id": 17,
        "paper_trading": True,
    }

    with TestClient(app) as client:
        response = client.post("/connection/connect", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    assert body["command_id"]

    assert commands, "expected connect command to be enqueued"
    command = commands[0]
    assert command["method"] == "connect"
    metadata_config = command["metadata"]["config"]
    assert metadata_config == {
        "host": "env-host",
        "port": 4102,
        "client_id": 12,
        "paper_trading": False,
    }
