import asyncio
from types import SimpleNamespace
from typing import Any, List

import pandas as pd
import pytest

from src.connection_manager import ConnectionConfig
from src.ibkr_client import IBKRClient

try:  # pragma: no cover - executed when real ibapi is installed
    from ibapi.common import BarData, HistoricalTickBidAsk
except ImportError:  # pragma: no cover - default for test environments
    from test.ibapi_stub import BarData, HistoricalTickBidAsk  # type: ignore


class _StubIB:
    def __init__(self) -> None:
        self.historical_calls: int = 0

    def isConnected(self) -> bool:  # noqa: N802 - ib_insync naming
        return True

    async def reqHistoricalDataAsync(self, *args: Any, **kwargs: Any) -> List[Any]:
        self.historical_calls += 1
        raise AssertionError("reqHistoricalDataAsync should not be called when ibapi controller is available")


class _StubConnectionManager:
    def __init__(self) -> None:
        self.config = ConnectionConfig(host="127.0.0.1", port=4002, client_id=1)
        self.ib = _StubIB()
        self._ibkr_client_ref = None

    def get_ib_instance(self):  # pragma: no cover - trivial
        return self.ib

    def is_connected(self) -> bool:
        return True

    def connect(self) -> bool:  # pragma: no cover - unused
        return True

    def disconnect(self) -> None:  # pragma: no cover - unused
        return None

    def run_in_connection_thread(self, func, *args, timeout=None, **kwargs):
        return func(*args, **kwargs)


class _FakeHistoricalController:
    def __init__(self, bars: List[BarData]) -> None:
        self._bars = bars
        self.calls: List[dict[str, Any]] = []

    def is_connected(self) -> bool:
        return True

    async def request_historical_data(self, contract, **kwargs: Any) -> List[BarData]:
        self.calls.append({"contract": contract, **kwargs})
        return list(self._bars)


class _FakeTickController:
    def __init__(self, ticks: List[HistoricalTickBidAsk]) -> None:
        self._ticks = ticks
        self.calls: List[dict[str, Any]] = []

    def is_connected(self) -> bool:
        return True

    async def request_historical_ticks(self, contract, **kwargs: Any) -> List[HistoricalTickBidAsk]:
        self.calls.append({"contract": contract, **kwargs})
        return list(self._ticks)


@pytest.fixture(autouse=True)
def _suppress_background_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(IBKRClient, "_setup_async_components", lambda self: None, raising=False)
    monkeypatch.setattr(IBKRClient, "_start_connection_monitor_task", lambda self: None, raising=False)


@pytest.mark.asyncio
async def test_get_historical_data_uses_ibapi_controller(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _StubConnectionManager()
    client = IBKRClient(connection_manager=manager)

    base_contract = SimpleNamespace(symbol="AAPL", secType="STK", exchange="SMART", currency="USD")

    monkeypatch.setattr(IBKRClient, "get_stock_contract", lambda self, symbol, exchange: base_contract, raising=False)
    monkeypatch.setattr(IBKRClient, "get_future_contract", lambda self, symbol, exchange: base_contract, raising=False)

    async def _qualify(self, contract, **kwargs):  # noqa: ANN001 - match signature
        return contract

    monkeypatch.setattr(IBKRClient, "_qualify_contract", _qualify, raising=False)

    bars = [
        BarData(date="20230101 00:00:00", open=100.0, high=101.0, low=99.0, close=100.5, volume=500),
        BarData(date="20230102 00:00:00", open=101.0, high=102.0, low=100.0, close=101.5, volume=400),
    ]
    controller = _FakeHistoricalController(bars)

    async def _ensure(self):
        return controller

    monkeypatch.setattr(IBKRClient, "_ensure_ibapi_controller", _ensure, raising=False)

    df = await client.get_historical_data("AAPL", duration="2 D", bar_size="1 hour")

    assert isinstance(df, pd.DataFrame)
    assert len(df) == 2
    assert controller.calls and controller.calls[0]["what_to_show"] == "TRADES"
    assert manager.ib.historical_calls == 0


@pytest.mark.asyncio
async def test_fetch_historical_ticks_ibapi(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _StubConnectionManager()
    client = IBKRClient(connection_manager=manager)

    contract = SimpleNamespace(symbol="ES", secType="FUT", exchange="CME", currency="USD")

    ticks = [
        HistoricalTickBidAsk(time=1672531200, priceBid=3999.0, priceAsk=3999.25, sizeBid=10.0, sizeAsk=11.0)
    ]
    controller = _FakeTickController(ticks)

    async def _ensure(self):
        return controller

    monkeypatch.setattr(IBKRClient, "_ensure_ibapi_controller", _ensure, raising=False)

    result = await client._fetch_historical_ticks_ibapi(
        contract,
        start="20230101 00:00:00",
        end="20230101 00:01:00",
        count=5,
        what_to_show="BID_ASK",
        use_rth=False,
        ignore_size=False,
        misc_options=[],
    )

    assert result == ticks
    assert controller.calls and controller.calls[0]["number_of_ticks"] == 5
