from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, List

import pytest

from src.ibkr_client import IBKRClient


class FakeHistoricalTickBidAsk:
    def __init__(
        self,
        *,
        time: datetime,
        price_bid: float,
        price_ask: float,
        size_bid: float,
        size_ask: float,
    ) -> None:
        self.time = time
        self.priceBid = price_bid
        self.priceAsk = price_ask
        self.sizeBid = size_bid
        self.sizeAsk = size_ask


class FakeIB:
    def __init__(self, ticks: List[Any]) -> None:
        self._ticks = list(ticks)
        self.request_history: list[dict[str, Any]] = []

    def isConnected(self) -> bool:
        return True

    def qualifyContracts(self, contract: Any) -> List[Any]:  # pragma: no cover - trivial
        return [contract]

    def reqHistoricalTicks(
        self,
        contract: Any,
        startDateTime: str,
        endDateTime: str,
        numberOfTicks: int,
        whatToShow: str,
        useRth: bool,
        ignoreSize: bool,
        miscOptions: list[Any],
    ) -> List[Any]:
        self.request_history.append(
            {
                "contract": contract,
                "start": startDateTime,
                "end": endDateTime,
                "count": numberOfTicks,
                "what": whatToShow,
            }
        )
        return list(self._ticks[: numberOfTicks or None])


class FakeIBWithoutHistory:
    def __init__(self) -> None:
        self.qualifyContracts_called = False

    def isConnected(self) -> bool:  # pragma: no cover - trivial
        return True

    def qualifyContracts(self, contract: Any) -> List[Any]:  # pragma: no cover - trivial
        self.qualifyContracts_called = True
        return [contract]


class StubConnectionManager:
    def __init__(self, ib: Any) -> None:
        self.ib = ib
        self._ibkr_client_ref = None
        self._ibkr_connect_in_progress = False
        self._connected = True

    def connect(self) -> bool:  # pragma: no cover - trivial
        self._connected = True
        return True

    def disconnect(self) -> None:  # pragma: no cover - trivial
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def run_in_connection_thread(self, func, *args, timeout=None, **kwargs):
        return func(*args, **kwargs)


@pytest.fixture(autouse=True)
def _suppress_async_components(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(IBKRClient, "_setup_async_components", lambda self: None)
    monkeypatch.setattr(IBKRClient, "_start_connection_monitor_task", lambda self: None)


def _build_client(ticks: list[FakeHistoricalTickBidAsk]) -> IBKRClient:
    manager = StubConnectionManager(FakeIB(ticks))
    return IBKRClient(connection_manager=manager)


def test_get_dom_history_returns_snapshots_in_order() -> None:
    base = datetime(2023, 1, 1, tzinfo=timezone.utc)
    ticks = [
        FakeHistoricalTickBidAsk(
            time=base + timedelta(minutes=idx),
            price_bid=4000.0 + idx,
            price_ask=4000.5 + idx,
            size_bid=10 + idx,
            size_ask=12 + idx,
        )
        for idx in range(3)
    ]

    client = _build_client(ticks)

    snapshots = client.get_dom_history("ES", limit=2)

    assert len(snapshots) == 2
    assert snapshots[0]["timestamp"] == (base + timedelta(minutes=1)).isoformat()
    assert snapshots[-1]["timestamp"] == (base + timedelta(minutes=2)).isoformat()
    assert snapshots[-1]["bids"][0]["price"] == pytest.approx(4002.0)
    assert snapshots[-1]["asks"][0]["size"] == pytest.approx(14.0)


def test_get_dom_history_respects_duration_window() -> None:
    base = datetime(2023, 1, 1, tzinfo=timezone.utc)
    ticks = [
        FakeHistoricalTickBidAsk(
            time=base + timedelta(minutes=offset),
            price_bid=100.0 + offset,
            price_ask=101.0 + offset,
            size_bid=1.0,
            size_ask=2.0,
        )
        for offset in range(6)
    ]

    client = _build_client(ticks)

    snapshots = client.get_dom_history("ES", duration="2 min", end_datetime=base + timedelta(minutes=5))

    timestamps = [item["timestamp"] for item in snapshots]
    assert timestamps[0] == (base + timedelta(minutes=3)).isoformat()
    assert timestamps[-1] == (base + timedelta(minutes=5)).isoformat()


def test_get_dom_history_raises_when_ib_lacks_ticks(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = StubConnectionManager(FakeIBWithoutHistory())
    client = IBKRClient(connection_manager=manager)

    with pytest.raises(NotImplementedError):
        client.get_dom_history("ES")
