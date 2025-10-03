"""Async coverage for :mod:`src.connection_manager`.

The real connection manager spins up background threads and talks to the
Interactive Brokers API.  The tests in this module replace those behaviours
with lightweight fakes so we can exercise the asyncio-facing code paths
without hitting the network.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import pytest

from src.connection_manager import ConnectionManager


@dataclass
class _FakeThread:
    """Minimal thread stand-in used by ``ConnectionManager`` tests.

    The production manager expects a thread-like object exposing ``is_alive``
    and ``join``.  The fake keeps track of whether ``join`` has been invoked so
    assertions can verify that stalled threads are cleaned up.
    """

    alive: bool = True

    def is_alive(self) -> bool:
        return self.alive

    def join(self, timeout: float | None = None) -> None:  # pragma: no cover -
        """Mirror ``threading.Thread.join`` without blocking."""

        # Mark the thread as stopped when ``join`` is called.  The timeout is
        # accepted purely for signature compatibility.
        self.alive = False


class _FakeIB:
    """Very small ``IB`` implementation that always reports as connected."""

    def __init__(self, connected: bool = True) -> None:
        self._connected = connected

    def isConnected(self) -> bool:  # noqa: N802 - match ib_insync naming
        return self._connected


async def _immediate_cpu_executor(func, *args, **kwargs):
    """Replace ``run_in_cpu_executor`` with an awaitable that executes inline."""

    return func(*args, **kwargs)


def _install_fake_prepare(manager: ConnectionManager) -> None:
    """Configure ``manager`` so ``_prepare_connection_start`` is deterministic."""

    def fake_prepare() -> str:
        manager._update_connection_state(ready=False, shutdown=False)
        with manager._connect_state_cond:
            manager._connect_in_progress = True
            manager._connect_attempt_started_at = time.monotonic()

        manager._connection_thread = _FakeThread()
        manager._connection_thread_alive = True
        manager._connection_shutdown_flag = False
        manager._last_connect_error = None
        manager.connected = False
        manager.ib = None
        return "started"

    manager._prepare_connection_start = fake_prepare  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_connect_async_times_out_when_thread_stalls(monkeypatch):
    manager = ConnectionManager()
    manager._connect_wait_limit_sec = 0.1

    monkeypatch.setattr(
        "src.connection_manager.run_in_cpu_executor", _immediate_cpu_executor
    )
    _install_fake_prepare(manager)

    success = await manager.connect_async(timeout=0.1)

    assert success is False
    error = manager.get_last_connect_error()
    assert error is not None and "timeout" in error.lower()
    with manager._connect_state_cond:
        assert manager._connect_in_progress is False
    assert manager._connection_thread is None


@pytest.mark.asyncio
async def test_ensure_connection_succeeds_and_disconnects(monkeypatch):
    manager = ConnectionManager()
    manager._connect_wait_limit_sec = 0.5

    monkeypatch.setattr(
        "src.connection_manager.run_in_cpu_executor", _immediate_cpu_executor
    )
    _install_fake_prepare(manager)

    async def trigger_ready() -> None:
        await asyncio.sleep(0)
        manager.ib = _FakeIB()
        manager.connected = True
        manager._update_connection_state(ready=True, shutdown=False)

    ready_task = asyncio.create_task(trigger_ready())
    result = await manager.ensure_connection(timeout=0.2)
    await ready_task

    assert result is True
    assert manager._connection_ready_flag is True
    assert manager.ib is not None
    event = manager._connection_ready_event
    assert event is not None and event.is_set()

    def fake_disconnect_sync() -> None:
        manager.connected = False
        manager.ib = None
        manager._connection_thread = None
        manager._update_connection_state(ready=False, shutdown=True)

    manager._connection_ready_flag = True
    manager._connection_shutdown_flag = False
    manager._connection_thread = _FakeThread()
    manager._disconnect_sync = fake_disconnect_sync  # type: ignore[attr-defined]

    await manager.disconnect_async(timeout=0.2)

    assert manager._connection_shutdown_flag is True
    assert manager._connection_ready_flag is False
    assert manager.ib is None
    assert manager.connected is False


@pytest.mark.asyncio
async def test_ensure_connection_short_circuits_when_ready(monkeypatch):
    manager = ConnectionManager()

    loop = asyncio.get_running_loop()
    manager.ib = _FakeIB()
    manager.connected = True
    manager._ensure_ready_event(loop)

    manager._update_connection_state(ready=True, shutdown=False)
    await asyncio.sleep(0)

    async def unexpected_connect(*_, **__):  # pragma: no cover - guard clause
        raise AssertionError("connect_async should not be called when ready")

    monkeypatch.setattr(manager, "connect_async", unexpected_connect)

    assert await manager.ensure_connection(timeout=0.1) is True
