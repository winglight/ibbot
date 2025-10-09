import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("ibapi.client")

from services.account.ib_client import ServiceIBClient


@pytest.fixture
async def prepared_client() -> ServiceIBClient:
    client = ServiceIBClient()
    loop = asyncio.get_running_loop()
    client._loop = loop  # type: ignore[attr-defined]
    client._connection_ready = asyncio.Event()  # type: ignore[attr-defined]
    client._connection_ready.set()  # type: ignore[attr-defined]
    client._callback_queue = asyncio.Queue()  # type: ignore[attr-defined]
    client._async_request_lock = asyncio.Lock()  # type: ignore[attr-defined]
    client.isConnected = lambda: True  # type: ignore[assignment]
    return client


@pytest.mark.asyncio
async def test_update_account_value_emits_event(prepared_client: ServiceIBClient) -> None:
    client = prepared_client
    client.updateAccountValue("NetLiquidation", "1234.56", "USD", "DU123")
    event_type, payload = await client.callback_queue.get()  # type: ignore[attr-defined]
    assert event_type == "account_update"
    assert payload.account == "DU123"
    assert payload.key == "NetLiquidation"
    assert str(payload.value) == "1234.56"
    assert payload.currency == "USD"


@pytest.mark.asyncio
async def test_subscribe_account_updates_tracks_accounts(prepared_client: ServiceIBClient) -> None:
    client = prepared_client
    calls: list[tuple[bool, str]] = []

    def _record(subscribe: bool, account: str) -> None:
        calls.append((subscribe, account))

    client.reqAccountUpdates = _record  # type: ignore[assignment]

    await client.subscribe_account_updates(["DU123", "DU456", "DU123"])
    assert (True, "DU123") in calls
    assert (True, "DU456") in calls
    assert len(calls) == 2

    await client.subscribe_account_updates(["DU123"])  # idempotent
    assert len(calls) == 2

    await client.unsubscribe_account_updates()
    assert calls[-1] == (False, "")


@pytest.mark.asyncio
async def test_fetch_account_summary_collects_values(prepared_client: ServiceIBClient) -> None:
    client = prepared_client
    req_calls: list[tuple[int, str, str]] = []
    cancel_calls: list[int] = []

    async def _next_request_id() -> int:
        return 42

    client.next_request_id = _next_request_id  # type: ignore[assignment]

    def _req_account_summary(req_id: int, group: str, tags: str) -> None:
        req_calls.append((req_id, group, tags))

    def _cancel(req_id: int) -> None:
        cancel_calls.append(req_id)

    client.reqAccountSummary = _req_account_summary  # type: ignore[assignment]
    client.cancelAccountSummary = _cancel  # type: ignore[assignment]

    async def _fulfil() -> None:
        await asyncio.sleep(0)
        client.accountSummary(42, "DU123", "NetLiquidation", "100.5", "USD")
        client.accountSummary(42, "DU123", "EquityWithLoanValue", "80.1", "USD")
        client.accountSummaryEnd(42)

    asyncio.create_task(_fulfil())
    result = await client.fetch_account_summary("NetLiquidation,EquityWithLoanValue")

    assert req_calls == [(42, "All", "NetLiquidation,EquityWithLoanValue")]
    assert cancel_calls == [42]
    assert result["accounts"]["DU123"]["NetLiquidation"] == pytest.approx(100.5)
    assert result["accounts"]["DU123"]["EquityWithLoanValue"] == pytest.approx(80.1)
    assert "received_at" in result


@pytest.mark.asyncio
async def test_fetch_positions_collects_events(prepared_client: ServiceIBClient) -> None:
    client = prepared_client
    client.reqPositions = lambda: None  # type: ignore[assignment]
    client.cancelPositions = lambda: None  # type: ignore[assignment]

    async def _fulfil() -> None:
        await asyncio.sleep(0)
        contract = SimpleNamespace(conId=1, symbol="AAPL", secType="STK", currency="USD")
        client.position("DU123", contract, 10.0, 100.0)
        client.positionEnd()

    asyncio.create_task(_fulfil())
    positions = await client.fetch_positions()
    assert len(positions) == 1
    assert positions[0].symbol == "AAPL"
    assert positions[0].position == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_fetch_daily_pnl_collects_values(prepared_client: ServiceIBClient) -> None:
    client = prepared_client
    client.reqPnL = lambda req_id, account, _: None  # type: ignore[assignment]
    client.cancelPnL = lambda req_id: None  # type: ignore[assignment]

    async def _next_request_id() -> int:
        return 7

    client.next_request_id = _next_request_id  # type: ignore[assignment]

    async def _fulfil() -> None:
        await asyncio.sleep(0)
        client.pnl(7, 12.5, 1.5, -0.25)

    asyncio.create_task(_fulfil())
    result = await client.fetch_daily_pnl("DU123")
    assert result["daily"] == pytest.approx(12.5)
    assert result["unrealized"] == pytest.approx(1.5)
    assert result["realized"] == pytest.approx(-0.25)


@pytest.mark.asyncio
async def test_fetch_historical_nav_collects_bars(prepared_client: ServiceIBClient) -> None:
    client = prepared_client
    client.reqHistoricalData = lambda *args, **kwargs: None  # type: ignore[assignment]
    client.cancelHistoricalData = lambda req_id: None  # type: ignore[assignment]

    async def _next_request_id() -> int:
        return 99

    client.next_request_id = _next_request_id  # type: ignore[assignment]

    bar = SimpleNamespace(
        date="20240101 12:00:00",
        open=1.0,
        high=2.0,
        low=0.5,
        close=1.5,
        volume=1000,
        average=1.2,
        barCount=5,
    )

    async def _fulfil() -> None:
        await asyncio.sleep(0)
        client.historicalData(99, bar)
        client.historicalDataEnd(99, "start", "end")

    asyncio.create_task(_fulfil())
    bars = await client.fetch_historical_nav(SimpleNamespace(), "1 D", "1 hour")
    assert len(bars) == 1
    assert bars[0]["close"] == pytest.approx(1.5)

