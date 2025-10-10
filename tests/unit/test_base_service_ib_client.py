from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("ibapi.client")

from src.ib.base_client import BaseServiceIBClient


class _DummyClient(BaseServiceIBClient):
    def __init__(self) -> None:
        super().__init__(
            service_name="test",
            host_env="TEST_IB_HOST",
            port_env="TEST_IB_PORT",
            client_id_env="TEST_IB_CLIENT_ID",
        )


@pytest.mark.asyncio
async def test_next_valid_id_signals_ready_event() -> None:
    client = _DummyClient()
    loop = asyncio.get_running_loop()
    ready_event = asyncio.Event()
    client._loop = loop  # type: ignore[attr-defined]
    client._connection_ready = ready_event  # type: ignore[attr-defined]

    client.nextValidId(1)
    await asyncio.sleep(0)

    assert ready_event.is_set()


@pytest.mark.asyncio
async def test_wait_until_ready_allows_unset_event() -> None:
    client = _DummyClient()
    loop = asyncio.get_running_loop()
    ready_event = asyncio.Event()
    client._connection_ready = ready_event  # type: ignore[attr-defined]

    loop.call_soon(ready_event.set)

    await client.wait_until_ready()


def test_connection_hosts_includes_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_IB_HOST", "ib-gateway")
    client = _DummyClient()

    assert client._connection_hosts() == ["ib-gateway", "127.0.0.1"]


def test_connection_ports_filters_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_IB_PORT", "4002,abc,0,4002")
    client = _DummyClient()

    assert client._connection_ports() == [4002]


def test_connection_ports_dedupes_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_IB_PORT", "4002, 7497, 7497")
    client = _DummyClient()

    assert client._connection_ports() == [4002, 7497]


def test_build_connection_targets_cartesian(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_IB_HOST", "ib-gateway, 127.0.0.1")
    monkeypatch.setenv("TEST_IB_PORT", "4002 7497")
    client = _DummyClient()

    assert client._build_connection_targets() == [
        ("ib-gateway", 4002),
        ("ib-gateway", 7497),
        ("127.0.0.1", 4002),
        ("127.0.0.1", 7497),
    ]
