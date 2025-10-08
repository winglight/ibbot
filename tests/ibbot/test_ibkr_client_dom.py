from __future__ import annotations

import asyncio
import threading
import types
from datetime import datetime, timedelta, timezone
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


class StubController:
    def __init__(self, ticks: List[Any]) -> None:
        self._ticks = list(ticks)

    def is_connected(self) -> bool:
        return True

    async def request_historical_ticks(
        self,
        contract: Any,
        *,
        start_datetime: str,
        end_datetime: str,
        number_of_ticks: int,
        what_to_show: str,
        use_rth: bool,
        ignore_size: bool,
        misc_options: List[Any],
        timeout: float,
    ) -> List[Any]:
        _ = (contract, start_datetime, end_datetime, what_to_show, use_rth, ignore_size, misc_options, timeout)
        return list(self._ticks[: number_of_ticks or None])


class StubConnectionManager:
    def __init__(self) -> None:
        self.config = types.SimpleNamespace(host="127.0.0.1", port=4002, client_id=1)
        self._connected = True

    def connect(self) -> bool:
        self._connected = True
        return True

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def get_ib_instance(self) -> None:
        return None

    def run_in_connection_thread(self, func, *args, timeout=None, **kwargs):
        _ = timeout
        return func(*args, **kwargs)


@pytest.fixture
def background_loop() -> asyncio.AbstractEventLoop:
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join()
        loop.close()


def _build_client(
    ticks: List[FakeHistoricalTickBidAsk],
    *,
    loop: asyncio.AbstractEventLoop,
) -> IBKRClient:
    manager = StubConnectionManager()
    client = IBKRClient(connection_manager=manager)

    controller = StubController(ticks)

    async def _ensure_controller(self: IBKRClient) -> StubController:
        self._ibapi_controller = controller
        return controller

    async def _qualify_contract(
        self: IBKRClient,
        contract: Any,
        *,
        symbol: str,
        exchange: str,
        currency: str,
    ) -> Any:
        _ = (symbol, exchange, currency)
        return contract

    client._async_loop = loop
    client._setup_async_components = lambda: None  # type: ignore[assignment]
    client._start_connection_monitor_task = lambda: None  # type: ignore[assignment]
    client._ensure_ibapi_controller = types.MethodType(_ensure_controller, client)
    client._qualify_contract = types.MethodType(_qualify_contract, client)

    return client


def test_get_dom_history_returns_snapshots_in_order(background_loop: asyncio.AbstractEventLoop) -> None:
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

    client = _build_client(ticks, loop=background_loop)

    snapshots = client.get_dom_history("ES", limit=2)

    assert len(snapshots) == 2
    assert snapshots[0]["timestamp"] == (base + timedelta(minutes=1)).isoformat()
    assert snapshots[-1]["timestamp"] == (base + timedelta(minutes=2)).isoformat()
    assert snapshots[-1]["bids"][0]["price"] == pytest.approx(4002.0)
    assert snapshots[-1]["asks"][0]["size"] == pytest.approx(14.0)


def test_get_dom_history_respects_duration_window(background_loop: asyncio.AbstractEventLoop) -> None:
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

    client = _build_client(ticks, loop=background_loop)

    snapshots = client.get_dom_history("ES", duration="2 min", end_datetime=base + timedelta(minutes=5))

    timestamps = [item["timestamp"] for item in snapshots]
    assert timestamps[0] == (base + timedelta(minutes=3)).isoformat()
    assert timestamps[-1] == (base + timedelta(minutes=5)).isoformat()


def test_get_dom_history_raises_when_controller_unavailable(background_loop: asyncio.AbstractEventLoop) -> None:
    manager = StubConnectionManager()
    client = IBKRClient(connection_manager=manager)

    client._async_loop = background_loop
    client._setup_async_components = lambda: None  # type: ignore[assignment]
    client._start_connection_monitor_task = lambda: None  # type: ignore[assignment]

    async def _ensure_controller(self: IBKRClient) -> None:
        return None

    client._ensure_ibapi_controller = types.MethodType(_ensure_controller, client)

    with pytest.raises(RuntimeError):
        client.get_dom_history("ES")
