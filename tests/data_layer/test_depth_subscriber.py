from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace

import pytest

from src.ib_async_adapter import DepthStream as AdapterDepthStream

from src.depth_subscriber import DepthSubscriber


class RecordingUpdateEvent(list):
    """Minimal stand in for the IB API event hook list."""

    def __iadd__(self, callback: Callable) -> "RecordingUpdateEvent":
        self.append(callback)
        return self


class RecordingIB:
    def __init__(self) -> None:
        self.req_calls: list[dict[str, object]] = []
        self.cancel_calls: list[object] = []

    def reqMktDepth(self, contract: object, *, numRows: int, isSmartDepth: bool) -> SimpleNamespace:
        ticker = SimpleNamespace(domBids=[], domAsks=[], updateEvent=RecordingUpdateEvent())
        self.req_calls.append(
            {
                "contract": contract,
                "num_rows": numRows,
                "is_smart_depth": isSmartDepth,
                "ticker": ticker,
            }
        )
        return ticker

    def cancelMktDepth(self, contract: object) -> None:
        self.cancel_calls.append(contract)


class RecordingConnectionManager:
    def __init__(self, *, connected: bool = True) -> None:
        self._connected = connected
        self.timeouts: list[float | None] = []
        self.calls: list[str] = []
        self.ib = RecordingIB()
        self.fail_next_call = False
        self._adapter = None
        self.async_timeouts: list[float | None] = []
        self.async_calls: list[str] = []

    def is_connected(self) -> bool:
        return self._connected

    def run_in_connection_thread(self, func: Callable, *args, timeout: float | None = None, **kwargs):
        self.calls.append(func.__name__)
        self.timeouts.append(timeout)
        if self.fail_next_call:
            self.fail_next_call = False
            return None
        return func(*args, **kwargs)

    def run_async_in_connection_thread(self, coro, timeout: float | None = None):
        self.async_timeouts.append(timeout)
        name = getattr(getattr(coro, "cr_code", None), "co_name", repr(coro))
        self.async_calls.append(name)
        return asyncio.run(coro)

    def get_ib_instance(self) -> RecordingIB:
        return self.ib


class RecordingIbClient:
    def __init__(self) -> None:
        self.contract_requests: list[str] = []

    def get_stock_contract(self, symbol: str) -> str:
        self.contract_requests.append(symbol)
        return f"CONTRACT:{symbol}"


class RecordingAdapter:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self.cancelled: list[object] = []

    def is_connected(self) -> bool:
        return True

    async def request_market_depth(self, contract: object, num_rows: int, *, is_smart_depth: bool) -> AdapterDepthStream:
        ticker = SimpleNamespace(
            domBids=[],
            domAsks=[],
            updateEvent=RecordingUpdateEvent(),
            domUpdateEvent=RecordingUpdateEvent(),
        )
        event = ticker.domUpdateEvent
        stream = AdapterDepthStream(contract=contract, handler=None, ticker=ticker, event=event)
        self.requests.append(
            {
                "contract": contract,
                "num_rows": num_rows,
                "is_smart_depth": is_smart_depth,
                "stream": stream,
            }
        )
        return stream

    async def cancel_market_depth(self, stream: AdapterDepthStream) -> None:
        self.cancelled.append(stream.contract)


def _no_op_callback(_: dict[str, object]) -> None:
    return None


def _shutdown_subscriber(subscriber: DepthSubscriber) -> None:
    executor = getattr(subscriber, "_callback_executor", None)
    try:
        subscriber._shutdown_executor()
    finally:
        if executor is not None:
            try:
                executor.shutdown(wait=True)
            except Exception:
                pass


def test_depth_subscriber_propagates_connection_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = RecordingConnectionManager()
    manager.fail_next_call = True
    subscriber = DepthSubscriber(manager)

    try:
        # Avoid touching the heavy IB resolution path when the connection thread fails.
        monkeypatch.setattr(DepthSubscriber, "_resolve_ibkr_client", lambda self: RecordingIbClient())

        result = subscriber.start_depth_stream("MNQ", _no_op_callback)

        assert result is False
        assert manager.calls == ["_subscribe"]
        assert manager.timeouts == [pytest.approx(15.0)]
    finally:
        _shutdown_subscriber(subscriber)


def test_force_restart_uses_cancel_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = RecordingConnectionManager()
    client = RecordingIbClient()
    subscriber = DepthSubscriber(manager)

    try:
        monkeypatch.setattr(DepthSubscriber, "_resolve_ibkr_client", lambda self: client)

        assert subscriber.start_depth_stream("AAPL", _no_op_callback) is True
        assert manager.timeouts == [pytest.approx(15.0)]

        assert subscriber.start_depth_stream("AAPL", _no_op_callback, force_restart=True) is True

        assert manager.timeouts == [pytest.approx(15.0), pytest.approx(5.0), pytest.approx(15.0)]
        assert manager.calls[1] == "_cancel_previous"
        assert manager.calls[2] == "_subscribe"
        assert manager.ib.cancel_calls == ["CONTRACT:AAPL"]
        assert client.contract_requests == ["AAPL", "AAPL"]
    finally:
        _shutdown_subscriber(subscriber)


def test_start_fails_when_connection_not_active(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = RecordingConnectionManager(connected=False)
    subscriber = DepthSubscriber(manager)

    try:
        monkeypatch.setattr(DepthSubscriber, "_resolve_ibkr_client", lambda self: RecordingIbClient())

        result = subscriber.start_depth_stream("AAPL", _no_op_callback)

        assert result is False
        error, status = subscriber.get_last_error()
        assert status == 400
        assert error is not None and "not active" in error
        assert manager.calls == []
        assert manager.timeouts == []
    finally:
        _shutdown_subscriber(subscriber)


def test_adapter_path_starts_and_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = RecordingConnectionManager()
    adapter = RecordingAdapter()
    manager._adapter = adapter
    subscriber = DepthSubscriber(manager)

    try:
        monkeypatch.setattr(DepthSubscriber, "_resolve_ibkr_client", lambda self: RecordingIbClient())

        captured: list[dict[str, object]] = []

        def _callback(payload: dict[str, object]) -> None:
            captured.append(payload)

        subscriber._callback_executor = None

        assert subscriber.start_depth_stream("AAPL", _callback, num_rows=10) is True

        assert manager.async_timeouts == [pytest.approx(15.0)]
        assert adapter.requests and adapter.requests[0]["num_rows"] == 10

        stream = subscriber._streams.get("AAPL")
        assert stream is not None
        assert stream.use_adapter is True
        assert stream.event is not None
        assert len(stream.event) == 1

        assert subscriber.stop_depth_stream("AAPL") is True
        assert manager.async_timeouts == [pytest.approx(15.0), pytest.approx(5.0)]
        assert adapter.cancelled == ["CONTRACT:AAPL"]
        assert captured == []
    finally:
        _shutdown_subscriber(subscriber)
