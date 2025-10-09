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
