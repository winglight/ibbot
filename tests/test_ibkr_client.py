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
