import asyncio
from types import SimpleNamespace
from typing import Any

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("ibapi.contract")

from services.market_data.ib_client import ServiceIBClient


def _build_contract(**overrides: Any) -> SimpleNamespace:
    defaults = {
        "conId": 123,
        "symbol": "ES",
        "secType": "FUT",
        "exchange": "GLOBEX",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.fixture()
async def prepared_client() -> ServiceIBClient:
    client = ServiceIBClient()
    loop = asyncio.get_running_loop()
    client._loop = loop  # type: ignore[attr-defined]
    ready = asyncio.Event()
    ready.set()
    client._connection_ready = ready  # type: ignore[attr-defined]
    client._callback_queue = asyncio.Queue()  # type: ignore[attr-defined]
    client._async_request_lock = asyncio.Lock()  # type: ignore[attr-defined]
    client.isConnected = lambda: True  # type: ignore[assignment]
    return client


@pytest.mark.asyncio
async def test_subscribe_quotes_deduplicates_requests(prepared_client: ServiceIBClient) -> None:
    client = prepared_client
    calls: list[tuple[int, bool, str]] = []
    cancels: list[int] = []

    async def _next_request_id() -> int:
        return 42

    client.next_request_id = _next_request_id  # type: ignore[assignment]
    client.reqMktData = lambda req_id, contract, ticks, snapshot, *_: calls.append(  # type: ignore[assignment]
        (req_id, snapshot, ticks)
    )
    client.cancelMktData = lambda req_id: cancels.append(req_id)  # type: ignore[assignment]

    contract = _build_contract(conId=10)
    first = await client.subscribe_quotes(contract, snapshot=False, tick_types=[1, 2])
    second = await client.subscribe_quotes(contract, snapshot=False, tick_types=[2, 1])

    assert first != second
    assert calls == [(42, False, "1,2")]

    still_active = await client.unsubscribe_quotes(first)
    assert still_active is True
    assert not cancels

    fully_cancelled = await client.unsubscribe_quotes(second)
    assert fully_cancelled is True
    assert cancels == [42]


@pytest.mark.asyncio
async def test_dom_subscriptions_reference_counts(prepared_client: ServiceIBClient) -> None:
    client = prepared_client
    req_ids = iter(range(100, 200))
    cancel_calls: list[int] = []

    async def _next_request_id() -> int:
        return next(req_ids)

    client.next_request_id = _next_request_id  # type: ignore[assignment]
    client.reqMktDepth = lambda req_id, *_: None  # type: ignore[assignment]
    client.cancelMktDepth = lambda req_id: cancel_calls.append(req_id)  # type: ignore[assignment]

    contract = _build_contract(conId=55)
    first = await client.subscribe_dom(contract, depth=5, exchange="GLOBEX")
    second = await client.subscribe_dom(contract, depth=5, exchange="GLOBEX")
    assert first != second

    await client.unsubscribe_dom(first)
    assert not cancel_calls

    await client.unsubscribe_dom(second)
    assert cancel_calls == [100]


@pytest.mark.asyncio
async def test_tick_price_events_forward_to_queue(prepared_client: ServiceIBClient) -> None:
    client = prepared_client

    async def _next_request_id() -> int:
        return 7

    client.next_request_id = _next_request_id  # type: ignore[assignment]
    client.reqMktData = lambda *_: None  # type: ignore[assignment]

    contract = _build_contract(conId=501)
    await client.subscribe_quotes(contract, snapshot=False, tick_types=[0])

    client.tickPrice(7, 1, 123.45, None)
    event_type, payload = await client.callback_queue.get()  # type: ignore[attr-defined]
    assert event_type == "market_tick"
    assert payload.contract_id == 501
    assert payload.value == pytest.approx(123.45)


@pytest.mark.asyncio
async def test_tick_by_tick_events_forward(prepared_client: ServiceIBClient) -> None:
    client = prepared_client

    async def _next_request_id() -> int:
        return 9

    client.next_request_id = _next_request_id  # type: ignore[assignment]
    client.reqTickByTickData = lambda *_: None  # type: ignore[assignment]

    contract = _build_contract(conId=222)
    await client.subscribe_tick_by_tick(contract, tick_type="Last")

    event_time = int(datetime.now(timezone.utc).timestamp())
    client.tickByTickAllLast(9, 0, event_time, 101.25, 3, None, "CME", "")

    event_type, payload = await client.callback_queue.get()  # type: ignore[attr-defined]
    assert event_type == "tick_by_tick"
    assert payload.contract_id == 222
    assert payload.price == pytest.approx(101.25)
    assert payload.size == pytest.approx(3)
    assert payload.exchange == "CME"
    assert payload.timestamp == datetime.fromtimestamp(event_time, tz=timezone.utc)


@pytest.mark.asyncio
async def test_real_time_bar_events_forward(prepared_client: ServiceIBClient) -> None:
    client = prepared_client

    async def _next_request_id() -> int:
        return 11

    client.next_request_id = _next_request_id  # type: ignore[assignment]
    client.reqRealTimeBars = lambda *_: None  # type: ignore[assignment]

    contract = _build_contract(conId=333)
    await client.subscribe_real_time_bars(contract, bar_size="5", what_to_show="TRADES")

    event_time = int(datetime.now(timezone.utc).timestamp())
    client.realtimeBar(11, event_time, 100.0, 101.0, 99.5, 100.5, 42, 100.2, 7)

    event_type, payload = await client.callback_queue.get()  # type: ignore[attr-defined]
    assert event_type == "real_time_bar"
    assert payload.contract_id == 333
    assert payload.open == pytest.approx(100.0)
    assert payload.high == pytest.approx(101.0)
    assert payload.low == pytest.approx(99.5)
    assert payload.close == pytest.approx(100.5)
    assert payload.volume == pytest.approx(42)
    assert payload.count == 7
    assert payload.timestamp == datetime.fromtimestamp(event_time, tz=timezone.utc)


@pytest.mark.asyncio
async def test_fetch_historical_collects_records(prepared_client: ServiceIBClient) -> None:
    client = prepared_client

    async def _next_request_id() -> int:
        return 88

    client.next_request_id = _next_request_id  # type: ignore[assignment]

    def _req_historical(req_id: int, *_: Any) -> None:
        async def _emit() -> None:
            await asyncio.sleep(0)
            bar = SimpleNamespace(
                date="20240101 12:00:00",
                open=100.0,
                high=105.0,
                low=99.5,
                close=102.0,
                volume=500,
                wap=101.2,
                barCount=4,
            )
            client.historicalData(req_id, bar)
            client.historicalDataEnd(req_id, "", "")

        asyncio.create_task(_emit())

    client.reqHistoricalData = _req_historical  # type: ignore[assignment]

    contract = _build_contract(conId=77)
    bars = await client.fetch_historical(
        contract,
        duration="1 D",
        bar_size="1 min",
        what_to_show="TRADES",
        keep_up_to_date=False,
    )

    assert len(bars) == 1
    record = bars[0]
    assert record["time"] == "20240101 12:00:00"
    assert record["high"] == pytest.approx(105.0)

