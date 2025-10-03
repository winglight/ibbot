"""Async tests for IBKRClient bridge integration.

The real IBKRClient coordinates with ConnectionManager.schedule_async to offload
order execution into the dedicated IBKR thread.  These tests provide lightweight
fakes so we can reason about the asyncio behaviour without spinning up the
entire connection stack.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import time
from types import SimpleNamespace
from typing import Awaitable, List, Tuple

import pytest

from src.connection_manager import BridgeTaskResult
from src.ibkr_client import IBKRClient


class _DummyIBSession:
    """Very small IB session stand-in used by the async bridge tests."""

    def __init__(self, *, delay: float = 0.05) -> None:
        self.delay = delay
        self._order_ids = itertools.count(1)
        self.qualify_calls: List[SimpleNamespace] = []
        self.order_calls: List[Tuple[SimpleNamespace, object]] = []

    async def qualifyContractsAsync(self, contract: SimpleNamespace):
        self.qualify_calls.append(contract)
        await asyncio.sleep(self.delay)
        return [contract]

    def placeOrder(self, contract: SimpleNamespace, order: object):
        order_id = next(self._order_ids)
        trade = SimpleNamespace(order=SimpleNamespace(orderId=order_id), contract=contract)
        self.order_calls.append((contract, order))
        return trade


class _ConcurrentTrackingManager:
    """Fake connection manager that records concurrent executions."""

    def __init__(self, ib_session: _DummyIBSession) -> None:
        self._ib_session = ib_session
        self.active_tasks = 0
        self.max_active = 0
        self.scheduled = 0

    def is_connected(self) -> bool:
        return True

    def get_ib_instance(self) -> _DummyIBSession:
        return self._ib_session

    async def schedule_async(self, coro: Awaitable, **_: object) -> BridgeTaskResult:
        self.scheduled += 1
        submitted_at = time.perf_counter()
        self.active_tasks += 1
        self.max_active = max(self.max_active, self.active_tasks)
        try:
            started_at = time.perf_counter()
            result = await coro
        finally:
            self.active_tasks -= 1
        execution_time = time.perf_counter() - started_at
        queue_wait = started_at - submitted_at
        return BridgeTaskResult(
            result=result,
            queue_wait=queue_wait,
            execution_time=execution_time,
            timed_out=False,
            error=None,
        )


class _TimeoutManager:
    """Fake connection manager that always times out the scheduled task."""

    def __init__(self, ib_session: _DummyIBSession) -> None:
        self._ib_session = ib_session
        self.cancelled_tasks = 0

    def is_connected(self) -> bool:
        return True

    def get_ib_instance(self) -> _DummyIBSession:
        return self._ib_session

    async def schedule_async(self, coro: Awaitable, **_: object) -> BridgeTaskResult:
        task = asyncio.create_task(coro)
        await asyncio.sleep(0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self.cancelled_tasks += 1
        return BridgeTaskResult(
            result=None,
            queue_wait=0.0,
            execution_time=0.0,
            timed_out=True,
            error=asyncio.TimeoutError("simulated bridge timeout"),
        )


@pytest.fixture
def ibkr_client(monkeypatch: pytest.MonkeyPatch) -> IBKRClient:
    """Provide a bare IBKRClient instance with trading disabled overrides."""

    monkeypatch.setattr("src.ibkr_client.is_trading_disabled", lambda: False)
    client = IBKRClient.__new__(IBKRClient)
    client.req_id_to_order_id = {}
    return client


@pytest.mark.asyncio
async def test_place_market_order_runs_concurrently_and_surfaces_metrics(ibkr_client: IBKRClient):
    ib_session = _DummyIBSession(delay=0.05)
    manager = _ConcurrentTrackingManager(ib_session)
    ibkr_client.connection_manager = manager

    orders = await asyncio.gather(
        ibkr_client.place_market_order("AAPL", 1, action="BUY"),
        ibkr_client.place_market_order("MSFT", 2, action="SELL"),
        ibkr_client.place_market_order("GOOG", 3, action="BUY"),
    )

    assert manager.max_active >= 2
    queue_waits = [metrics["queue_wait"] for _, metrics in orders]
    execution_times = [metrics["execution_time"] for _, metrics in orders]
    assert all(wait >= 0.0 for wait in queue_waits)
    assert all(exec_time >= ib_session.delay - 0.01 for exec_time in execution_times)
    assert len(ibkr_client.req_id_to_order_id) == len(orders)
    assert {str(trade.order.orderId) for trade, _ in orders} == set(
        ibkr_client.req_id_to_order_id.values()
    )


@pytest.mark.asyncio
async def test_place_market_order_propagates_timeout_from_bridge(ibkr_client: IBKRClient):
    ib_session = _DummyIBSession(delay=0.01)
    manager = _TimeoutManager(ib_session)
    ibkr_client.connection_manager = manager

    with pytest.raises(asyncio.TimeoutError):
        await ibkr_client.place_market_order("AAPL", 1)

    assert manager.cancelled_tasks == 1
