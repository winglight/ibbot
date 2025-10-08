from __future__ import annotations

import pytest

from src.connection_manager import ConnectionConfig
from src.ibkr_client import IBKRClient

try:  # pragma: no cover - exercised when ib_insync is installed locally
    from ib_insync import Stock  # type: ignore
except ImportError:  # pragma: no cover - fallback for CI/test environments
    from test.ib_insync_stub import Stock  # type: ignore

try:  # pragma: no cover - executed only when the real ibapi package is present
    from ibapi.contract import Contract as IBAPIContract, ContractDetails as IBAPIContractDetails
except ImportError:  # pragma: no cover - default path during unit tests
    from test.ibapi_stub import Contract as IBAPIContract, ContractDetails as IBAPIContractDetails  # type: ignore


class _DummyIB:
    def __init__(self) -> None:
        self.calls: int = 0

    def isConnected(self) -> bool:  # noqa: N802 - ib_insync naming
        return True

    async def qualifyContractsAsync(self, contract):  # pragma: no cover - fallback path
        self.calls += 1
        return [contract]


class _DummyConnectionManager:
    def __init__(self) -> None:
        self.config = ConnectionConfig(host="127.0.0.1", port=4002, client_id=1)
        self.ib = _DummyIB()
        self._ibkr_client_ref = None

    def get_ib_instance(self):
        return self.ib

    def is_connected(self) -> bool:
        return True

    def connect(self) -> bool:  # pragma: no cover - unused in tests
        return True

    def disconnect(self) -> None:  # pragma: no cover - unused in tests
        return None


class _FakeController:
    def __init__(self) -> None:
        self.calls: int = 0

    def is_connected(self) -> bool:
        return True

    async def request_contract_details(self, contract, *, timeout: float = 5.0):
        self.calls += 1
        api_contract = IBAPIContract()
        for attr, value in (
            ("symbol", getattr(contract, "symbol", "")),
            ("secType", getattr(contract, "secType", "")),
            ("exchange", getattr(contract, "exchange", "")),
            ("currency", getattr(contract, "currency", "")),
            ("conId", 1234),
        ):
            setattr(api_contract, attr, value)
        details = IBAPIContractDetails(contract=api_contract)
        return [details]


@pytest.mark.asyncio
async def test_contract_details_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(IBKRClient, "_start_connection_monitor_task", lambda self: None, raising=False)
    manager = _DummyConnectionManager()
    client = IBKRClient(connection_manager=manager)

    fake_controller = _FakeController()

    async def _fake_ensure(self):
        return fake_controller

    monkeypatch.setattr(IBKRClient, "_ensure_ibapi_controller", _fake_ensure, raising=False)

    contract = Stock("AAPL")
    qualified = await client._qualify_contract(contract, symbol="AAPL", exchange="SMART", currency="USD")

    assert qualified is contract
    assert getattr(qualified, "conId", None) == 1234
    assert fake_controller.calls == 1
    assert manager.ib.calls == 0

    second_contract = Stock("AAPL")
    qualified_second = await client._qualify_contract(
        second_contract,
        symbol="AAPL",
        exchange="SMART",
        currency="USD",
    )

    assert getattr(qualified_second, "conId", None) == 1234
    assert fake_controller.calls == 1  # cached result
    assert manager.ib.calls == 0  # ib_insync fallback unused
