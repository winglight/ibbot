import asyncio
from typing import Any

import pytest

from src.ibapi_controller import IBAPIController
from src.ibkr_client import IBKRClient

try:  # pragma: no cover - exercised in environments with real ibapi
    from ibapi.contract import Contract
except ImportError:  # pragma: no cover - fallback used in CI/unit tests
    from test.ibapi_stub import Contract  # type: ignore


@pytest.mark.asyncio
async def test_place_order_prefers_ibapi_controller(monkeypatch: Any) -> None:
    client = IBKRClient()
    controller = IBAPIController(loop=asyncio.get_running_loop())

    await controller.connect()
    await controller.wait_until_connected()

    async def fake_qualify(contract: Contract, **_: Any) -> Contract:
        return contract

    async def ensure_controller() -> IBAPIController:
        return controller

    monkeypatch.setattr(client, "_qualify_contract", fake_qualify)
    monkeypatch.setattr(client, "_ensure_ibapi_controller", ensure_controller)

    try:
        result = await client.place_order(
            symbol="ES",
            quantity=1,
            action="BUY",
            order_type="MARKET",
            exchange="CME",
            sec_type="FUT",
        )
    finally:
        await controller.disconnect()
        client.disconnect()

    assert result is not None
    assert result["order_type"] == "MARKET"
    assert int(result["quantity"]) == 1
    assert result["status"]


@pytest.mark.asyncio
async def test_cancel_order_uses_ibapi_controller(monkeypatch: Any) -> None:
    client = IBKRClient()
    controller = IBAPIController(loop=asyncio.get_running_loop())

    await controller.connect()
    await controller.wait_until_connected()

    async def fake_qualify(contract: Contract, **_: Any) -> Contract:
        return contract

    async def ensure_controller() -> IBAPIController:
        return controller

    monkeypatch.setattr(client, "_qualify_contract", fake_qualify)
    monkeypatch.setattr(client, "_ensure_ibapi_controller", ensure_controller)

    try:
        result = await client.place_order(
            symbol="NQ",
            quantity=1,
            action="SELL",
            order_type="LIMIT",
            limit_price=15000.0,
            exchange="CME",
            sec_type="FUT",
        )
        assert result is not None
        order_id = result["order_id"]

        success, message = await client.cancel_order(order_id)
        assert success is True
        assert "cancel" in message.lower()
    finally:
        await controller.disconnect()
        client.disconnect()
