from __future__ import annotations

import asyncio
import importlib
import sys
import types
from types import SimpleNamespace
from typing import Any

import pytest

import src.ibkr_client as ibkr_module


class _OrdersClientStub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, float]]] = []

    async def update_order_fill_metrics(self, order_id: str, payload: dict[str, float]) -> dict[str, Any]:
        self.calls.append((order_id, dict(payload)))
        return {"success": True, "order": {"id": order_id, **payload}}


def test_order_fill_cache_cleared_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _OrdersClientStub()
    monkeypatch.setattr(ibkr_module, "_ORDERS_SERVICE_CLIENT", stub)
    monkeypatch.setattr(ibkr_module, "_get_orders_service_client", lambda: stub)

    client = ibkr_module.IBKRClient.__new__(ibkr_module.IBKRClient)  # type: ignore[call-arg]
    client._order_fill_cache = {}

    asyncio.run(client._update_order_commission_pnl("ABC", 3.2, 1.1))

    assert stub.calls == [("ABC", {"commission": 3.2, "realized_pnl": 1.1})]
    assert "ABC" not in client._order_fill_cache


def test_refresh_account_invokes_expected_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ibkr_module.IBKRClient.__new__(ibkr_module.IBKRClient)  # type: ignore[call-arg]

    calls: list[tuple[str, Any]] = []

    async def fake_ensure_connection(*, reason: str) -> None:
        calls.append(("ensure", reason))

    async def fake_get_account_info() -> dict[str, Any]:
        calls.append(("account", None))
        return {"account_id": "DU123"}

    async def fake_get_positions(*, fast: bool) -> list[dict[str, Any]]:
        calls.append(("positions", fast))
        return [{"symbol": "ES", "position": 1}]

    monkeypatch.setattr(client, "ensure_connection", fake_ensure_connection)
    monkeypatch.setattr(client, "get_account_info", fake_get_account_info)
    monkeypatch.setattr(client, "get_positions", fake_get_positions)

    result = asyncio.run(client.refresh_account())

    assert result["account_info"] == {"account_id": "DU123"}
    assert result["positions"] == [{"symbol": "ES", "position": 1}]
    assert result["success"] is True
    assert isinstance(result["refreshed_at"], str)

    assert calls == [
        ("ensure", "refresh-account"),
        ("account", None),
        ("positions", True),
    ]


def test_refresh_account_respects_fast_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ibkr_module.IBKRClient.__new__(ibkr_module.IBKRClient)  # type: ignore[call-arg]

    async def fake_ensure_connection(*, reason: str) -> None:
        return None

    async def fake_get_account_info() -> dict[str, Any]:
        return {}

    captured_fast: list[bool] = []

    async def fake_get_positions(*, fast: bool) -> list[dict[str, Any]]:
        captured_fast.append(fast)
        return []

    monkeypatch.setattr(client, "ensure_connection", fake_ensure_connection)
    monkeypatch.setattr(client, "get_account_info", fake_get_account_info)
    monkeypatch.setattr(client, "get_positions", fake_get_positions)

    result = asyncio.run(client.refresh_account(fast_positions=False))

    assert result["positions"] == []
    assert result["success"] is False
    assert captured_fast == [False]


class _FakeConnectionManager:
    def __init__(self, ib: Any) -> None:
        self._ib = ib

    def connect(self) -> bool:
        return True

    def disconnect(self) -> None:
        return None

    def is_connected(self) -> bool:
        return True

    def get_last_connect_error(self) -> str | None:
        return None

    def get_ib_instance(self) -> Any:
        return self._ib


class _FakeIB:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def managedAccounts(self) -> list[str]:
        self.calls.append(("managedAccounts", tuple()))
        return ["DU123"]

    def accountSummary(self, account: str | None = None) -> list[SimpleNamespace]:
        if not account:
            raise TypeError("account argument required")
        self.calls.append(("accountSummary", (account,)))
        return [
            SimpleNamespace(account=account, tag="NetLiquidation", value="100000", currency="USD")
        ]

    def accountValues(self, account: str | None = None) -> list[SimpleNamespace]:
        if not account:
            raise TypeError("account argument required")
        self.calls.append(("accountValues", (account,)))
        return [
            SimpleNamespace(account=account, tag="TotalCashValue", value="50000", currency="USD")
        ]

    def isConnected(self) -> bool:
        return True


@pytest.mark.anyio
async def test_get_account_info_uses_managed_account(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_ib = _FakeIB()
    client = ibkr_module.IBKRClient(connection_manager=_FakeConnectionManager(fake_ib))

    async def ensure_connection_stub(*, reason: str | None = None) -> None:
        return None

    monkeypatch.setattr(client, "ensure_connection", ensure_connection_stub)
    monkeypatch.setattr(client, "get_ib_instance", lambda: fake_ib)

    result = await client.get_account_info()

    assert result["account_summary"] == [
        {
            "account": "DU123",
            "tag": "NetLiquidation",
            "value": "100000",
            "currency": "USD",
        }
    ]
    assert result["account_values"] == [
        {
            "account": "DU123",
            "tag": "TotalCashValue",
            "value": "50000",
            "currency": "USD",
        }
    ]
    assert fake_ib.calls == [
        ("managedAccounts", tuple()),
        ("accountSummary", ("DU123",)),
        ("accountValues", ("DU123",)),
    ]


def test_simple_client_account_info_uses_managed_account(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_ib = _FakeIB()

    remove_stub = False
    if "ib_async" not in sys.modules:
        stub = types.ModuleType("ib_async")
        stub.IB = object
        stub.Stock = object
        stub.Contract = object
        stub.util = types.SimpleNamespace()
        sys.modules["ib_async"] = stub
        remove_stub = True

    simple_module = importlib.import_module("src.simple_ibkr_client")
    simple_client = simple_module.SimpleIBKRClient()
    simple_client.ib = fake_ib

    result = simple_client.get_account_info()

    assert result["account_summary"][0]["account"] == "DU123"
    assert result["account_values"][0]["account"] == "DU123"
    assert fake_ib.calls == [
        ("managedAccounts", tuple()),
        ("accountSummary", ("DU123",)),
        ("accountValues", ("DU123",)),
    ]

    if remove_stub:
        sys.modules.pop("ib_async", None)
