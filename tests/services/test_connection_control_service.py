from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.connection_control import main as service_module


class DummyRedis:
    async def close(self) -> None:  # pragma: no cover - compatibility shim
        return None


class DummyProvider:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.modules: dict[str, dict[str, Any]] = {}

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def get_module(self, module: str, *, use_cache: bool = True) -> dict[str, Any]:
        return dict(self.modules.get(module, {}))

    async def set_module(self, module: str, values: dict[str, Any]) -> dict[str, Any]:
        self.modules[module] = dict(values)
        return dict(values)

    async def get(self, module: str, key: str, *, default: Any = None, use_cache: bool = True) -> Any:
        return default


@pytest.fixture(autouse=True)
def _patch_dependencies(monkeypatch: pytest.MonkeyPatch):
    provider = DummyProvider()
    monkeypatch.setattr(service_module, "ConfigProvider", lambda *args, **kwargs: provider)

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

    return {"provider": provider, "commands": commands}


def test_connection_config_roundtrip(monkeypatch: pytest.MonkeyPatch, _patch_dependencies) -> None:
    provider: DummyProvider = _patch_dependencies["provider"]
    provider.modules[service_module.CONFIG_MODULE] = {
        "host": "redis-host",
        "port": "4002",
        "client_id": "3",
        "paper_trading": "false",
    }

    app = service_module.create_app(health_interval=0)

    with TestClient(app) as client:
        response = client.get("/connection/config")

    assert response.status_code == 200
    assert response.json() == {
        "host": "redis-host",
        "port": 4002,
        "client_id": 3,
        "paper_trading": False,
    }


def test_connect_endpoint_persists_config_and_enqueues(_patch_dependencies) -> None:
    provider: DummyProvider = _patch_dependencies["provider"]
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

    # ensure config persisted via provider
    stored = provider.modules[service_module.CONFIG_MODULE]
    assert stored["host"] == payload["host"]

    assert commands, "expected connect command to be enqueued"
    command = commands[0]
    assert command["method"] == "connect"
    assert command["metadata"]["config"]["client_id"] == payload["client_id"]
