from __future__ import annotations

import asyncio
from http import HTTPStatus
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from src.routes import connection as connection_module
from src.connection_manager import ConnectionConfig


class FakeConnectionManager:
    def __init__(self) -> None:
        self.config = ConnectionConfig(host="127.0.0.1", port=4002, client_id=7, paper_trading=True)
        self.connected = False
        self.disconnect_calls = 0
        self.connect_calls = 0
        self.last_error: str | None = None
        self.disconnect_should_fail = False
        self.connect_should_fail = False

    def get_config(self) -> ConnectionConfig:
        return self.config

    def update_config(self, config: ConnectionConfig) -> None:
        self.config = config

    def disconnect(self) -> bool:
        self.disconnect_calls += 1
        if self.disconnect_should_fail:
            raise RuntimeError("disconnect failed")
        self.connected = False
        return True

    def connect(self) -> bool:
        self.connect_calls += 1
        if self.connect_should_fail:
            self.last_error = "connect failed"
            raise RuntimeError("connect failed")
        self.connected = True
        self.last_error = None
        return True

    def is_connected(self) -> bool:
        return self.connected

    def get_last_connect_error(self) -> str | None:
        return self.last_error


class DummyLoop:
    def __init__(self, env: SimpleNamespace) -> None:
        self.env = env
        self.executor_calls: list[Callable[[], Any]] = []

    def run_in_executor(self, executor: Any, func: Callable[[], Any]):
        self.executor_calls.append(func)
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        try:
            result = func()
        except Exception as exc:  # pragma: no cover - defensive guard
            future.set_exception(exc)
        else:
            future.set_result(result)
        return future


@pytest.fixture
def connection_env(monkeypatch):
    manager = FakeConnectionManager()
    events: list[dict[str, Any]] = []
    set_client_calls: list[Any] = []

    class FakeWebsocketManager:
        async def broadcast(self, payload: dict[str, Any]) -> None:
            events.append(payload)

    class FakeIBKRClient:
        async def get_account_info(self) -> dict[str, Any]:
            return {"account": "DU123"}

    async def wait_for_stub(awaitable, timeout):
        env.wait_for_calls.append({"timeout": timeout})
        if env.wait_for_side_effects:
            effect = env.wait_for_side_effects.pop(0)
            if isinstance(effect, Exception):
                raise effect
            if callable(effect):
                return await effect(awaitable, timeout)
        return await awaitable

    async def to_thread_stub(func: Callable, *args, **kwargs):
        return func(*args, **kwargs)

    def get_ibkr_client_stub():
        return None

    def set_ibkr_client_stub(client: Any) -> None:
        set_client_calls.append(client)

    env = SimpleNamespace(
        manager=manager,
        events=events,
        set_client_calls=set_client_calls,
        wait_for_calls=[],
        wait_for_side_effects=[],
    )
    env.loop = DummyLoop(env)

    monkeypatch.setattr(connection_module, "connection_manager", manager, raising=False)
    monkeypatch.setattr(connection_module, "websocket_manager", FakeWebsocketManager(), raising=False)
    monkeypatch.setattr(connection_module, "IBKRClient", FakeIBKRClient, raising=False)
    monkeypatch.setattr(connection_module, "get_ibkr_client", get_ibkr_client_stub, raising=False)
    monkeypatch.setattr(connection_module, "set_ibkr_client", set_ibkr_client_stub, raising=False)
    monkeypatch.setattr(connection_module.asyncio, "get_event_loop", lambda: env.loop)
    monkeypatch.setattr(connection_module.asyncio, "wait_for", wait_for_stub)
    monkeypatch.setattr(connection_module.asyncio, "to_thread", to_thread_stub)

    return env


def test_get_connection_config(client, connection_env):
    response = client.get("/api/connection_config")
    assert response.status_code == HTTPStatus.OK
    data = response.json()
    assert data["host"] == "127.0.0.1"
    assert data["port"] == 4002
    assert data["client_id"] == 7
    assert data["paper_trading"] is True


def test_connect_reconnect_flow(client, admin_headers, connection_env):
    manager: FakeConnectionManager = connection_env.manager

    response = client.post(
        "/api/connect",
        headers=admin_headers,
        json={"host": "127.0.0.1", "port": 4002, "client_id": 8, "paper_trading": False},
    )

    assert response.status_code == HTTPStatus.OK
    assert manager.disconnect_calls == 1
    assert manager.connect_calls == 1
    assert manager.connected is True
    event_types = [event.get("type") for event in connection_env.events]
    assert "connection_status" in event_types
    assert "account_update" in event_types
    assert connection_env.set_client_calls, "expected set_ibkr_client to be called"


def test_connect_disconnect_timeout_returns_408(client, admin_headers, connection_env):
    connection_env.wait_for_side_effects.append(asyncio.TimeoutError())

    response = client.post(
        "/api/connect",
        headers=admin_headers,
        json={"host": "127.0.0.1", "port": 4002, "client_id": 9, "paper_trading": True},
    )

    assert response.status_code == HTTPStatus.REQUEST_TIMEOUT
    assert response.json()["detail"] == "Disconnect request timed out"


def test_connection_status_falls_back_to_cached_state(client, admin_headers, connection_env):
    manager: FakeConnectionManager = connection_env.manager
    manager.connected = True
    manager.last_error = None
    connection_env.wait_for_side_effects.append(asyncio.TimeoutError())

    response = client.get("/api/connection_status", headers=admin_headers)

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["connected"] is True
    assert payload["account_type"] == "Paper"
    assert connection_env.wait_for_calls[0]["timeout"] == pytest.approx(1.0)
