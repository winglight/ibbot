"""Integration tests for FastAPI startup initialisation."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict

import pytest
from httpx import ASGITransport, AsyncClient

import sys
import types
from dataclasses import dataclass


if "src.connection_manager" not in sys.modules:
    @dataclass
    class _ConnectionConfigStub:
        host: str = "127.0.0.1"
        port: int = 4002
        client_id: int = 1
        paper_trading: bool = True

    class _StubConnectionManager:
        def __init__(self) -> None:
            self.connected = False
            self.config = _ConnectionConfigStub()
            self._last_error: str | None = None

        def get_config(self) -> _ConnectionConfigStub:
            return self.config

        def update_config(self, config: _ConnectionConfigStub) -> None:
            self.config = config

        def connect(self) -> bool:
            self.connected = False
            return False

        def disconnect(self) -> None:
            self.connected = False

        def is_connected(self) -> bool:
            return self.connected

        def get_last_connect_error(self) -> str | None:
            return self._last_error

    _connection_manager_stub = _StubConnectionManager()
    class _LoggerStub:
        def debug(self, *args: Any, **kwargs: Any) -> None:
            return None

        info = warning = error = debug

    stub_module = types.ModuleType("src.connection_manager")
    stub_module.connection_manager = _connection_manager_stub
    stub_module.ConnectionConfig = _ConnectionConfigStub
    stub_module.connection_logger = _LoggerStub()
    sys.modules["src.connection_manager"] = stub_module

import core
from src.ib_command_health import IB_COMMAND_HEALTH_KEY
from main import app


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeRedis:
    def __init__(self) -> None:
        self._data: Dict[str, Dict[str, str]] = {}

    async def ping(self) -> bool:  # pragma: no cover - behaviour trivial
        return True

    async def hset(self, key: str, mapping: Dict[str, str]) -> None:
        bucket = self._data.setdefault(key, {})
        bucket.update({k: str(v) for k, v in mapping.items()})

    async def hgetall(self, key: str) -> Dict[str, str]:
        return dict(self._data.get(key, {}))

    async def close(self) -> None:  # pragma: no cover - compatibility hook
        self._data.clear()


class DummyProvider:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._started = False

    async def start(self) -> None:
        self._started = True

    async def close(self) -> None:
        self._started = False

    async def get_module(self, module: str, *, use_cache: bool = True) -> Dict[str, str]:
        assert module == "services"
        return {}


@pytest.mark.anyio("asyncio")
async def test_startup_initialises_core_utilities(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_redis = FakeRedis()

    async def fake_init_redis(*args: Any, **kwargs: Any) -> FakeRedis:
        return fake_redis

    monkeypatch.setattr("core.container.init_redis", fake_init_redis)
    monkeypatch.setattr("core.container.configure_influx", lambda **_: None)
    monkeypatch.setattr("core.container.ConfigProvider", DummyProvider)

    await core.shutdown()

    async with app.router.lifespan_context(app):
        container = core.require_container()
        assert container.redis_client is fake_redis
        assert container.websocket_manager is core.websocket_manager
        provider = container.config_provider
        assert provider is not None and getattr(provider, "_started", False)
        assert container.telegram_notifier is not None
        assert container.notification_manager is not None
        assert container.order_manager is not None
        assert container.risk_manager is not None and getattr(
            container.risk_manager, "monitoring_active", False
        )
        assert container.strategy_manager is not None
        assert container.strategy_risk_manager is not None
        assert container.queue_manager is not None and container.queue_manager.is_running
        assert container.account_snapshot_service is not None

    await core.shutdown()


@pytest.mark.anyio("asyncio")
async def test_startup_allows_disabling_embedded_risk_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_redis = FakeRedis()

    async def fake_init_redis(*args: Any, **kwargs: Any) -> FakeRedis:
        return fake_redis

    monkeypatch.setenv("ENABLE_EMBEDDED_RISK_MANAGER", "0")
    monkeypatch.setattr("core.container.init_redis", fake_init_redis)
    monkeypatch.setattr("core.container.configure_influx", lambda **_: None)
    monkeypatch.setattr("core.container.ConfigProvider", DummyProvider)

    await core.shutdown()

    async with app.router.lifespan_context(app):
        container = core.require_container()
        risk_manager = container.risk_manager
        assert risk_manager is not None
        assert not getattr(risk_manager, "monitoring_active", False)

    await core.shutdown()


@pytest.mark.anyio("asyncio")
async def test_app_routes_available_without_ib(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_redis = FakeRedis()

    async def fake_init_redis(*args: Any, **kwargs: Any) -> FakeRedis:
        return fake_redis

    monkeypatch.setattr("core.container.init_redis", fake_init_redis)
    monkeypatch.setattr("core.container.configure_influx", lambda **_: None)
    monkeypatch.setattr("core.container.ConfigProvider", DummyProvider)
    await core.shutdown()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login_response = await client.post(
            "/api/login",
            json={"username": "admin", "password": "change_me"},
        )
        assert login_response.status_code == 200
        token = login_response.json().get("access_token")
        assert token
        headers = {"Authorization": f"Bearer {token}"}

        index_response = await client.get("/")
        assert index_response.status_code == 200

        status_response = await client.get("/api/connection_status", headers=headers)
        assert status_response.status_code == 200
        payload = status_response.json()
        assert payload.get("connected") is False
        assert payload.get("source") == "http"
        assert "account_type" in payload
        assert payload.get("stale") is False

        docs_response = await client.get("/api-docs.json")
        assert docs_response.status_code == 200

    await core.shutdown()


@pytest.mark.anyio("asyncio")
async def test_connection_status_prefers_redis_when_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_redis = FakeRedis()

    async def fake_init_redis(*args: Any, **kwargs: Any) -> FakeRedis:
        return fake_redis

    monkeypatch.setattr("core.container.init_redis", fake_init_redis)
    monkeypatch.setattr("core.container.configure_influx", lambda **_: None)
    monkeypatch.setattr("core.container.ConfigProvider", DummyProvider)
    await core.shutdown()

    services = await core.initialise(force=True)
    assert services.redis_client is fake_redis

    now_iso = datetime.now(timezone.utc).isoformat()
    await fake_redis.hset(
        IB_COMMAND_HEALTH_KEY,
        {
            "last_seen": now_iso,
            "queue_depth": "0",
            "last_error": "",
        },
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        login_response = await client.post(
            "/api/login",
            json={"username": "admin", "password": "change_me"},
        )
        assert login_response.status_code == 200
        token = login_response.json().get("access_token")
        assert token
        headers = {"Authorization": f"Bearer {token}"}

        await fake_redis.hset(
            IB_COMMAND_HEALTH_KEY,
            {
                "last_seen": now_iso,
                "queue_depth": "0",
                "last_error": "",
            },
        )

        status_response = await client.get("/api/connection_status", headers=headers)
        assert status_response.status_code == 200
        payload = status_response.json()
        assert payload.get("source") == "redis", payload
        assert payload.get("connected") is True
        assert payload.get("stale") is False
        assert payload.get("queue_depth") == 0
        assert payload.get("last_error") is None
        assert isinstance(payload.get("last_seen"), str)

    await core.shutdown()
