import asyncio
import time
from types import SimpleNamespace

import pytest

from src import connection_manager
from src.ib_async_adapter import IBAsyncAdapter


class _DummyEvent:
    def __init__(self) -> None:
        self._handlers = []

    def __iadd__(self, handler):
        if handler not in self._handlers:
            self._handlers.append(handler)
        return self

    def __isub__(self, handler):
        if handler in self._handlers:
            self._handlers.remove(handler)
        return self

    def fire(self) -> None:
        for handler in list(self._handlers):
            handler()


class TimeoutProneAdapter:
    call_history: list[int] = []

    def __init__(self, client_factory=None):  # pragma: no cover - signature compatibility
        self.wrapper = SimpleNamespace(register_error_handler=lambda handler: None)
        self._connected = False
        self._attempt = 0
        self._disconnect_event = _DummyEvent()
        self._client_state = SimpleNamespace(value=0)
        self._client = SimpleNamespace(connState=self._client_state)
        self._native_client = SimpleNamespace(
            client=self._client,
            disconnectedEvent=self._disconnect_event,
            connectionClosed=self._disconnect_event,
            disconnected_event=self._disconnect_event,
        )

    async def connect(self, host: str, port: int, client_id: int, timeout: float, **_kwargs):
        self._attempt += 1
        TimeoutProneAdapter.call_history.append(client_id)
        await asyncio.sleep(0)
        if self._attempt == 1:
            raise asyncio.TimeoutError("connect timed out")
        if self._attempt == 2:
            # handshake never flips to connected within grace period
            self._connected = False
            self._client_state.value = 1
            return
        self._connected = True
        self._client_state.value = 2

    async def disconnect(self):
        self._connected = False
        self._client_state.value = 0
        await asyncio.sleep(0)

    def is_connected(self) -> bool:
        return self._connected

    @property
    def native_client(self):
        return self._native_client

    async def set_market_data_type(self, *_args, **_kwargs):
        return None


@pytest.fixture(autouse=True)
def configure_env(monkeypatch):
    monkeypatch.setenv("IBKR_CLIENT_ID_MIN", "1")
    monkeypatch.setenv("IBKR_CLIENT_ID_MAX", "4")
    monkeypatch.setenv("IBKR_CONNECT_ASYNC_TIMEOUT_SEC", "0.05")
    monkeypatch.setenv("IBKR_CONNECT_HANDSHAKE_GRACE_SEC", "0.05")
    monkeypatch.setenv("IBKR_CONNECT_HANDSHAKE_POLL_INTERVAL_SEC", "0.01")
    monkeypatch.setenv("IBKR_CONNECTED_HEALTH_CHECK_INTERVAL_SEC", "0.1")
    yield
    TimeoutProneAdapter.call_history.clear()


def test_retries_different_client_ids_on_timeout(monkeypatch):
    monkeypatch.setattr(connection_manager, "IBAsyncAdapter", TimeoutProneAdapter)
    monkeypatch.setattr(connection_manager, "setup_event_loop_policy", lambda: None)

    manager = connection_manager.ConnectionManager()
    try:
        assert manager.connect() is True
        # allow background thread to settle
        for _ in range(20):
            if TimeoutProneAdapter.call_history and manager.connected:
                break
            time.sleep(0.01)
        assert TimeoutProneAdapter.call_history[:3] == [1, 2, 3]
        assert manager.config.client_id == 3
    finally:
        manager.disconnect()


class AsyncOnlyClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def disconnectAsync(self) -> None:
        self.calls.append("disconnectAsync")


@pytest.mark.asyncio
async def test_adapter_disconnect_supports_async_aliases():
    client = AsyncOnlyClient()
    adapter = IBAsyncAdapter(client=client)

    await adapter.disconnect()

    assert client.calls == ["disconnectAsync"]


@pytest.mark.asyncio
async def test_adapter_disconnect_clears_client_state():
    from test.ib_insync_stub import IB as StubIB

    client = StubIB()
    adapter = IBAsyncAdapter(client=client)
    adapter._connection_args = {"host": "ib", "port": 4002}

    await adapter.disconnect()

    assert adapter.native_client is None
    assert adapter._connection_args == {}
