import asyncio
from typing import Any

import asyncio
import pytest

from src.ibapi_controller import IBAPIController
from src.ibkr_client import IBKRClient

try:  # pragma: no cover - exercised in environments with real ibapi
    from ibapi.contract import Contract
except ImportError:  # pragma: no cover - fallback used in CI/unit tests
    from test.ibapi_stub import Contract  # type: ignore


@pytest.mark.asyncio
async def test_place_order_prefers_ibapi_controller(monkeypatch: Any) -> None:
    monkeypatch.setenv("IBBOT_DISABLE_TRADING", "0")
    monkeypatch.setattr("src.ibkr_client.is_trading_disabled", lambda: False)
    client = IBKRClient()
    controller = IBAPIController(loop=asyncio.get_running_loop())

    await controller.connect()
    await controller.wait_until_connected()

    async def fake_qualify(contract: Contract, **_: Any) -> Contract:
        return contract

    monkeypatch.setattr(client, "_qualify_contract", fake_qualify)
    client._ibapi_controller = controller
    await client._trade_book.attach(controller)

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
        await client.disconnect()

    assert result is not None
    assert result["order_type"] == "MARKET"
    assert int(result["quantity"]) == 1
    assert result["status"]


@pytest.mark.asyncio
async def test_cancel_order_uses_ibapi_controller(monkeypatch: Any) -> None:
    monkeypatch.setenv("IBBOT_DISABLE_TRADING", "0")
    monkeypatch.setattr("src.ibkr_client.is_trading_disabled", lambda: False)
    client = IBKRClient()
    controller = IBAPIController(loop=asyncio.get_running_loop())

    await controller.connect()
    await controller.wait_until_connected()

    async def fake_qualify(contract: Contract, **_: Any) -> Contract:
        return contract

    monkeypatch.setattr(client, "_qualify_contract", fake_qualify)
    client._ibapi_controller = controller
    await client._trade_book.attach(controller)

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
        await client.disconnect()


@pytest.mark.asyncio
async def test_get_orders_reflects_trade_book_snapshot(monkeypatch: Any) -> None:
    monkeypatch.setenv("IBBOT_DISABLE_TRADING", "0")
    monkeypatch.setattr("src.ibkr_client.is_trading_disabled", lambda: False)
    client = IBKRClient()
    controller = IBAPIController(loop=asyncio.get_running_loop())

    await controller.connect()
    await controller.wait_until_connected()

    async def fake_qualify(contract: Contract, **_: Any) -> Contract:
        return contract

    monkeypatch.setattr(client, "_qualify_contract", fake_qualify)
    client._ibapi_controller = controller
    await client._trade_book.attach(controller)

    try:
        result = await client.place_order(
            symbol="MES",
            quantity=2,
            action="BUY",
            order_type="LIMIT",
            limit_price=4200.5,
            exchange="CME",
            sec_type="FUT",
        )
        assert result is not None

        await asyncio.sleep(0)

        orders = client.get_orders()
        assert any(entry["order_id"] == result["order_id"] for entry in orders)
    finally:
        await controller.disconnect()
        await client.disconnect()


@pytest.mark.asyncio
async def test_cancel_all_orders_uses_trade_book(monkeypatch: Any) -> None:
    monkeypatch.setenv("IBBOT_DISABLE_TRADING", "0")
    monkeypatch.setattr("src.ibkr_client.is_trading_disabled", lambda: False)
    client = IBKRClient()
    controller = IBAPIController(loop=asyncio.get_running_loop())

    await controller.connect()
    await controller.wait_until_connected()

    async def fake_qualify(contract: Contract, **_: Any) -> Contract:
        return contract

    monkeypatch.setattr(client, "_qualify_contract", fake_qualify)
    client._ibapi_controller = controller
    await client._trade_book.attach(controller)

    try:
        for symbol, price in (("ES", 4300.0), ("NQ", 15050.0)):
            await client.place_order(
                symbol=symbol,
                quantity=1,
                action="SELL",
                order_type="LIMIT",
                limit_price=price,
                exchange="CME",
                sec_type="FUT",
            )

        await asyncio.sleep(0)

        cancelled = await client.cancel_all_orders()
        assert cancelled == 2
    finally:
        await controller.disconnect()
        await client.disconnect()
