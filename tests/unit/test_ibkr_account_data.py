import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from src.connection_manager import ConnectionConfig
from src.ibkr_client import IBKRClient


class _StubIB:
    def isConnected(self) -> bool:  # noqa: N802 - legacy naming
        return True

    def accountValues(self):  # pragma: no cover - should not be invoked
        raise AssertionError("accountValues should not be called when controller is available")

    def accountSummary(self):  # pragma: no cover - should not be invoked
        raise AssertionError("accountSummary should not be called when controller is available")

    def portfolio(self):  # pragma: no cover - should not be invoked
        raise AssertionError("portfolio should not be called when controller is available")


class _StubConnectionManager:
    def __init__(self) -> None:
        self.config = ConnectionConfig(host="127.0.0.1", port=4002, client_id=1)
        self.config.account = "DU123"
        self.config.paper_trading = True
        self.ib = _StubIB()

    def run_in_connection_thread(self, func, *args, **kwargs):  # pragma: no cover - should not run
        raise AssertionError("run_in_connection_thread should not be used with controller snapshots")

    def is_connected(self) -> bool:
        return True

    def get_ib_instance(self):  # pragma: no cover - unused
        return self.ib


class _FakeAccountController:
    def __init__(self) -> None:
        self.summary_calls: List[Dict[str, Any]] = []
        self.update_calls: List[Dict[str, Any]] = []

    def is_connected(self) -> bool:
        return True

    async def request_account_updates(self, *, account: str, timeout: float) -> Dict[str, Any]:
        self.update_calls.append({"account": account, "timeout": timeout})
        contract = SimpleNamespace(symbol="MES", multiplier="5", secType="FUT", exchange="CME", currency="USD")
        return {
            "account": account or "DU123",
            "account_values": [
                {"tag": "AccountCode", "value": "DU123", "currency": "USD", "account": "DU123"},
                {"tag": "TotalCashValue", "value": "10000", "currency": "USD", "account": "DU123"},
                {"tag": "AvailableFunds", "value": "8000", "currency": "USD", "account": "DU123"},
                {"tag": "BuyingPower", "value": "32000", "currency": "USD", "account": "DU123"},
                {"tag": "NetLiquidation", "value": "12000", "currency": "USD", "account": "DU123"},
                {"tag": "UnrealizedPnL", "value": "500", "currency": "USD", "account": "DU123"},
                {"tag": "RealizedPnL", "value": "50", "currency": "USD", "account": "DU123"},
            ],
            "portfolio": [
                {
                    "contract": contract,
                    "position": 2.0,
                    "market_price": 4200.0,
                    "market_value": 42000.0,
                    "average_cost": 20800.0,
                    "unrealized_pnl": 400.0,
                    "realized_pnl": 0.0,
                    "account": account or "DU123",
                }
            ],
        }

    async def request_account_summary(self, *, group: str, tags: str, timeout: float) -> List[Dict[str, Any]]:
        self.summary_calls.append({"group": group, "tags": tags, "timeout": timeout})
        return [
            {"account": "DU123", "tag": "TotalCashValue", "value": "10000", "currency": "USD"},
            {"account": "DU123", "tag": "NetLiquidation", "value": "12000", "currency": "USD"},
        ]


@pytest.fixture(autouse=True)
def _suppress_background_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(IBKRClient, "_setup_async_components", lambda self: None, raising=False)
    monkeypatch.setattr(IBKRClient, "_start_connection_monitor_task", lambda self: None, raising=False)


@pytest.mark.asyncio
async def test_get_account_info_uses_controller(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _StubConnectionManager()
    client = IBKRClient(connection_manager=manager)

    controller = _FakeAccountController()

    async def _ensure(self):
        return controller

    async def _fail_active_ib(self, **kwargs):  # pragma: no cover - should not run
        raise AssertionError("_ensure_active_ib should not be invoked when controller is available")

    monkeypatch.setattr(IBKRClient, "_ensure_ibapi_controller", _ensure, raising=False)
    monkeypatch.setattr(IBKRClient, "_ensure_active_ib", _fail_active_ib, raising=False)

    info = await client.get_account_info()

    assert info["account_id"] == "DU123"
    assert info["total_cash"] == 10000.0
    assert info["available_funds"] == 8000.0
    assert info["buying_power"] == 32000.0
    assert client._cached_account_info["net_liquidation"] == 12000.0
    assert controller.update_calls


@pytest.mark.asyncio
async def test_get_account_summary_uses_controller(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _StubConnectionManager()
    client = IBKRClient(connection_manager=manager)

    controller = _FakeAccountController()

    async def _ensure(self):
        return controller

    async def _fail_active_ib(self, **kwargs):  # pragma: no cover - should not run
        raise AssertionError("_ensure_active_ib should not be invoked when controller is available")

    monkeypatch.setattr(IBKRClient, "_ensure_ibapi_controller", _ensure, raising=False)
    monkeypatch.setattr(IBKRClient, "_ensure_active_ib", _fail_active_ib, raising=False)

    summary = await client.get_account_summary()

    assert any(item["tag"] == "TotalCashValue" for item in summary)
    assert controller.summary_calls


@pytest.mark.asyncio
async def test_get_positions_uses_controller_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _StubConnectionManager()
    client = IBKRClient(connection_manager=manager)

    controller = _FakeAccountController()

    async def _ensure(self):
        return controller

    async def _fail_active_ib(self, **kwargs):  # pragma: no cover - should not run
        raise AssertionError("_ensure_active_ib should not be invoked when controller is available")

    monkeypatch.setattr(IBKRClient, "_ensure_ibapi_controller", _ensure, raising=False)
    monkeypatch.setattr(IBKRClient, "_ensure_active_ib", _fail_active_ib, raising=False)

    positions = await client.get_positions(fast=True)

    assert positions
    assert positions[0]["symbol"] == "MES"
    assert positions[0]["unrealized_pnl"] == pytest.approx(400.0)
    assert controller.update_calls
