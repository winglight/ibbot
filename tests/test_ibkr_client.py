from __future__ import annotations

import asyncio
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
