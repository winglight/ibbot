from __future__ import annotations

import asyncio
from contextlib import suppress
from types import SimpleNamespace

import pytest

from src import ib_client_registry as registry
from src.ibkr_client import IBKRClient, IBKRConnectionError


@pytest.fixture(autouse=True)
def _disable_monitor_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(IBKRClient, "_start_connection_monitor_task", lambda self: None)


class _StubConnectionManager:
    def __init__(self, *, connect_result: bool = True) -> None:
        self._connected = False
        self._connecting = False
        self.connect_calls = 0
        self.connect_result = connect_result
        self.config = SimpleNamespace(client_id=0)
        self._ibkr_client_ref = None

    def is_connected(self) -> bool:
        return self._connected

    def is_connecting(self) -> bool:
        return self._connecting

    def wait_for_connection(self, _timeout: float) -> bool:
        return False

    def connect(self) -> bool:
        self.connect_calls += 1
        if not self.connect_result:
            return False
        self._connected = True
        return True

    def disconnect(self) -> None:
        self._connected = False

    def get_ib_instance(self) -> None:  # pragma: no cover - compatibility hook
        return None


@pytest.mark.asyncio
async def test_ensure_connection_invokes_connect_on_demand() -> None:
    manager = _StubConnectionManager()
    client = IBKRClient(connection_manager=manager)

    assert manager.connect_calls == 0

    await client.ensure_connection(reason="test")
    assert manager.connect_calls == 1

    await client.ensure_connection(reason="test")
    assert manager.connect_calls == 1

    await _cleanup_client(client)


@pytest.mark.asyncio
async def test_ensure_connection_raises_when_connect_fails() -> None:
    manager = _StubConnectionManager(connect_result=False)
    client = IBKRClient(connection_manager=manager)

    with pytest.raises(IBKRConnectionError):
        await client.ensure_connection(reason="failure")

    assert manager.connect_calls == 1

    await _cleanup_client(client)


def _install_registry_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_build(env: str, default: int) -> _StubConnectionManager:
        manager = _StubConnectionManager()
        manager.config.client_id = default
        return manager

    monkeypatch.setattr(registry, "_build_connection_manager", _fake_build)


@pytest.mark.asyncio
async def test_registry_returns_shared_instances(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_registry_stubs(monkeypatch)
    await registry.shutdown()

    primary_first = await registry.get_client("primary")
    primary_second = await registry.get_client("primary")
    assert primary_first is primary_second
    assert primary_first.metadata.get("purpose") == "command"

    secondary_first = await registry.get_client("secondary")
    secondary_second = await registry.get_client("secondary")
    assert secondary_first is secondary_second
    assert secondary_first is not primary_first

    await _cleanup_client(primary_first)
    await _cleanup_client(secondary_first)
    await registry.shutdown()


def test_registry_sync_accessor(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_registry_stubs(monkeypatch)
    asyncio.run(registry.shutdown())

    primary_first = registry.get_client_sync("primary")
    primary_second = registry.get_client_sync("primary")
    assert primary_first is primary_second

    asyncio.run(_cleanup_client(primary_first))
    asyncio.run(registry.shutdown())


async def _cleanup_client(client: IBKRClient) -> None:
    client.disconnect()
    task = getattr(client, "_connection_monitor_task", None)
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

