import asyncio

import pytest

try:  # pragma: no cover - executed when real ibapi is available
    from ibapi.common import BarData, HistoricalTickBidAsk
    from ibapi.contract import Contract, ContractDetails
except ImportError:  # pragma: no cover - fallback for test environments
    from test.ibapi_stub import (  # type: ignore
        BarData,
        Contract,
        ContractDetails,
        HistoricalTickBidAsk,
        Order,
        OrderState,
    )

from src.ibapi_controller import ConnectionParams, IBAPIController, IBAPIRequestError


@pytest.mark.asyncio
async def test_connect_sets_connected_flag() -> None:
    controller = IBAPIController(loop=asyncio.get_running_loop())

    order_id = await controller.connect()
    await controller.wait_until_connected()

    assert controller.is_connected() is True
    assert order_id == 1

    await controller.disconnect()


@pytest.mark.asyncio
async def test_request_contract_details_success() -> None:
    controller = IBAPIController(loop=asyncio.get_running_loop())

    await controller.connect()
    await controller.wait_until_connected()

    contract = Contract()
    request_task = asyncio.create_task(controller.request_contract_details(contract, timeout=1))
    await asyncio.sleep(0)

    details = ContractDetails(contract=contract)
    controller.contractDetails(1, details)
    controller.contractDetailsEnd(1)

    result = await request_task
    assert result == [details]

    await controller.disconnect()


@pytest.mark.asyncio
async def test_request_contract_details_error_propagates() -> None:
    controller = IBAPIController(loop=asyncio.get_running_loop())

    await controller.connect()
    await controller.wait_until_connected()

    contract = Contract()
    request_task = asyncio.create_task(controller.request_contract_details(contract, timeout=1))
    await asyncio.sleep(0)

    controller.error(1, 200, "No security definition")

    with pytest.raises(IBAPIRequestError):
        await request_task

    await controller.disconnect()
    assert controller.is_connected() is False


@pytest.mark.asyncio
async def test_reconnect_reuses_updated_params() -> None:
    controller = IBAPIController(loop=asyncio.get_running_loop())

    await controller.connect(params=ConnectionParams(host="localhost", port=4002, client_id=7))
    await controller.wait_until_connected()

    # Trigger a reconnect using the stored parameters
    await controller.reconnect()
    await controller.wait_until_connected()

    assert controller.is_connected() is True

    await controller.disconnect()


@pytest.mark.asyncio
async def test_error_events_are_forwarded() -> None:
    controller = IBAPIController(loop=asyncio.get_running_loop())

    await controller.connect()
    await controller.wait_until_connected()

    # 连接建立时会先推送 connectAck 事件
    await asyncio.wait_for(controller.event_queue.get(), timeout=1)

    controller.error(42, 500, "boom")
    event = await asyncio.wait_for(controller.event_queue.get(), timeout=1)

    assert event == {
        "type": "error",
        "request_id": 42,
        "code": 500,
        "message": "boom",
        "advanced": "",
    }

    await controller.disconnect()


@pytest.mark.asyncio
async def test_tick_events_forwarded_into_queue() -> None:
    controller = IBAPIController(loop=asyncio.get_running_loop())

    await controller.connect()
    await controller.wait_until_connected()

    # Drain the initial connect ack event
    await asyncio.wait_for(controller.event_queue.get(), timeout=1)

    contract = Contract()
    req_id = await controller.request_market_data(contract)

    client = controller.client
    client.emit_tick_price(req_id, 1, 101.25)
    event = await asyncio.wait_for(controller.event_queue.get(), timeout=1)
    assert event["type"] == "tickPrice"
    assert event["request_id"] == req_id
    assert event["price"] == 101.25

    client.emit_tick_size(req_id, 0, 5)
    size_event = await asyncio.wait_for(controller.event_queue.get(), timeout=1)
    assert size_event == {"type": "tickSize", "request_id": req_id, "tick_type": 0, "size": 5}

    await controller.cancel_market_data(req_id)
    await controller.disconnect()


@pytest.mark.asyncio
async def test_depth_events_forwarded_into_queue() -> None:
    controller = IBAPIController(loop=asyncio.get_running_loop())

    await controller.connect()
    await controller.wait_until_connected()

    await asyncio.wait_for(controller.event_queue.get(), timeout=1)

    contract = Contract()
    depth_req_id = await controller.request_market_depth(contract, num_rows=5)

    client = controller.client
    client.emit_depth_update(depth_req_id, 0, "MM", 0, 1, 100.5, 2.0)
    event = await asyncio.wait_for(controller.event_queue.get(), timeout=1)
    assert event["type"] == "marketDepth"
    assert event["request_id"] == depth_req_id
    assert event["side"] == 1
    assert event["price"] == 100.5

    await controller.cancel_market_depth(depth_req_id)
    await controller.disconnect()


@pytest.mark.asyncio
async def test_request_historical_data_collects_bars() -> None:
    controller = IBAPIController(loop=asyncio.get_running_loop())

    await controller.connect()
    await controller.wait_until_connected()

    contract = Contract()
    task = asyncio.create_task(
        controller.request_historical_data(
            contract,
            end_datetime="",
            duration="1 D",
            bar_size="1 hour",
            what_to_show="TRADES",
            use_rth=False,
            timeout=1,
        )
    )

    await asyncio.sleep(0)

    bar = BarData(date="20230101 12:00:00", open=100.0, high=105.0, low=95.0, close=102.0, volume=1000.0)
    controller.client.emit_historical_bar(1, bar)
    controller.client.emit_historical_data_end(1)

    result = await task
    assert result == [bar]

    await controller.disconnect()


@pytest.mark.asyncio
async def test_request_historical_data_error_propagates() -> None:
    controller = IBAPIController(loop=asyncio.get_running_loop())

    await controller.connect()
    await controller.wait_until_connected()

    contract = Contract()
    task = asyncio.create_task(
        controller.request_historical_data(
            contract,
            end_datetime="",
            duration="1 D",
            bar_size="1 hour",
            what_to_show="TRADES",
            use_rth=False,
            timeout=1,
        )
    )

    await asyncio.sleep(0)

    controller.error(1, 420, "historical data error")

    with pytest.raises(IBAPIRequestError):
        await task

    await controller.disconnect()


@pytest.mark.asyncio
async def test_request_historical_ticks_collects_snapshots() -> None:
    controller = IBAPIController(loop=asyncio.get_running_loop())

    await controller.connect()
    await controller.wait_until_connected()

    contract = Contract()
    task = asyncio.create_task(
        controller.request_historical_ticks(
            contract,
            start_datetime="20230101 00:00:00",
            end_datetime="20230101 00:01:00",
            number_of_ticks=10,
            what_to_show="BID_ASK",
            use_rth=False,
            ignore_size=False,
            timeout=1,
        )
    )

    await asyncio.sleep(0)

    tick = HistoricalTickBidAsk(time=1672531200, priceBid=100.0, priceAsk=100.5, sizeBid=5.0, sizeAsk=6.0)
    controller.client.emit_historical_ticks(1, [tick], done=True)

    result = await task
    assert result == [tick]

    await controller.disconnect()


@pytest.mark.asyncio
async def test_request_historical_ticks_error_propagates() -> None:
    controller = IBAPIController(loop=asyncio.get_running_loop())

    await controller.connect()
    await controller.wait_until_connected()

    contract = Contract()
    task = asyncio.create_task(
        controller.request_historical_ticks(
            contract,
            start_datetime="20230101 00:00:00",
            end_datetime="20230101 00:01:00",
            number_of_ticks=10,
            what_to_show="BID_ASK",
            use_rth=False,
            ignore_size=False,
            timeout=1,
        )
    )

    await asyncio.sleep(0)

    controller.error(1, 504, "historical ticks error")

    with pytest.raises(IBAPIRequestError):
        await task

    await controller.disconnect()


@pytest.mark.asyncio
async def test_allocate_order_id_returns_incrementing_ids() -> None:
    controller = IBAPIController(loop=asyncio.get_running_loop())

    await controller.connect()
    await controller.wait_until_connected()

    first = await controller.allocate_order_id()
    second = await controller.allocate_order_id()

    assert first == 1
    assert second == 2

    await controller.disconnect()


@pytest.mark.asyncio
async def test_place_order_pushes_open_order_event() -> None:
    controller = IBAPIController(loop=asyncio.get_running_loop())

    await controller.connect()
    await controller.wait_until_connected()

    # Drain initial connect acknowledgement event
    await asyncio.wait_for(controller.event_queue.get(), timeout=1)

    contract = Contract(symbol="ES", exchange="CME", currency="USD")
    order = Order(action="BUY", totalQuantity=1, orderType="MKT")

    placement = await controller.place_order(contract, order, timeout=1)

    assert placement.order_id == 1
    assert placement.order is order

    open_event = await asyncio.wait_for(controller.event_queue.get(), timeout=1)
    assert open_event["type"] == "openOrder"
    assert open_event["order_id"] == 1

    status_event = await asyncio.wait_for(controller.event_queue.get(), timeout=1)
    assert status_event["type"] == "orderStatus"
    assert status_event["order_id"] == 1
    assert status_event["status"].lower() == "submitted"

    await controller.disconnect()


@pytest.mark.asyncio
async def test_cancel_order_acknowledged_via_order_status() -> None:
    controller = IBAPIController(loop=asyncio.get_running_loop())

    await controller.connect()
    await controller.wait_until_connected()

    await asyncio.wait_for(controller.event_queue.get(), timeout=1)

    contract = Contract(symbol="ES", exchange="CME", currency="USD")
    order = Order(action="SELL", totalQuantity=2, orderType="LMT", lmtPrice=4200.0)

    placement = await controller.place_order(contract, order, timeout=1)

    # Drain acknowledgement events from placement
    await asyncio.wait_for(controller.event_queue.get(), timeout=1)
    await asyncio.wait_for(controller.event_queue.get(), timeout=1)

    status = await controller.cancel_order(placement.order_id, timeout=1)

    assert status.lower() == "cancelled"

    cancel_event = await asyncio.wait_for(controller.event_queue.get(), timeout=1)
    assert cancel_event["type"] == "orderStatus"
    assert cancel_event["order_id"] == placement.order_id
    assert cancel_event["status"].lower() == "cancelled"

    await controller.disconnect()


@pytest.mark.asyncio
async def test_request_account_summary_collects_values() -> None:
    controller = IBAPIController(loop=asyncio.get_running_loop())

    await controller.connect()
    await controller.wait_until_connected()

    task = asyncio.create_task(controller.request_account_summary(timeout=1))
    await asyncio.sleep(0)

    client = controller.client
    client.emit_account_summary(1, "DU123", "TotalCashValue", "1000", "USD")
    client.emit_account_summary(1, "DU123", "BuyingPower", "4000", "USD")
    client.emit_account_summary_end(1)

    result = await task
    assert {
        "account": "DU123",
        "tag": "TotalCashValue",
        "value": "1000",
        "currency": "USD",
    } in result
    assert {
        "account": "DU123",
        "tag": "BuyingPower",
        "value": "4000",
        "currency": "USD",
    } in result

    await controller.disconnect()


@pytest.mark.asyncio
async def test_request_account_updates_returns_snapshot() -> None:
    controller = IBAPIController(loop=asyncio.get_running_loop())

    await controller.connect()
    await controller.wait_until_connected()

    task = asyncio.create_task(controller.request_account_updates(account="DU123", timeout=1))
    await asyncio.sleep(0)

    client = controller.client
    contract = Contract(symbol="AAPL", secType="STK", exchange="SMART", currency="USD")
    client.emit_account_value("NetLiquidation", "15000", "USD", "DU123")
    client.emit_portfolio_update(contract, 10.0, 150.0, 1500.0, 1200.0, 300.0, 0.0, "DU123")
    client.emit_account_time("20231001 12:00:00")
    client.emit_account_download_end("DU123")

    snapshot = await task
    assert snapshot["account"] == "DU123"
    assert snapshot["account_values"]
    assert snapshot["portfolio"]
    assert snapshot["account_time"] == "20231001 12:00:00"
    assert client._account_updates_subscribed is False

    await controller.disconnect()


@pytest.mark.asyncio
async def test_request_account_updates_ignores_other_accounts() -> None:
    controller = IBAPIController(loop=asyncio.get_running_loop())

    await controller.connect()
    await controller.wait_until_connected()

    task = asyncio.create_task(controller.request_account_updates(account="DU123", timeout=1))
    await asyncio.sleep(0)

    client = controller.client
    contract = Contract(symbol="AAPL", secType="STK", exchange="SMART", currency="USD")
    client.emit_account_value("NetLiquidation", "5000", "USD", "OTHER")
    client.emit_portfolio_update(contract, 5.0, 120.0, 600.0, 100.0, 20.0, 0.0, "OTHER")
    client.emit_account_value("NetLiquidation", "15000", "USD", "DU123")
    client.emit_portfolio_update(contract, 10.0, 150.0, 1500.0, 1200.0, 300.0, 0.0, "DU123")
    client.emit_account_download_end("DU123")

    snapshot = await task
    assert snapshot["account"] == "DU123"
    assert all(item["account"] == "DU123" for item in snapshot["account_values"])
    assert all(item["account"] == "DU123" for item in snapshot["portfolio"])

    await controller.disconnect()


@pytest.mark.asyncio
async def test_request_account_updates_uses_default_account_when_missing() -> None:
    controller = IBAPIController(loop=asyncio.get_running_loop())

    await controller.connect()
    await controller.wait_until_connected()

    task = asyncio.create_task(controller.request_account_updates(timeout=1))
    await asyncio.sleep(0)

    client = controller.client
    client.emit_managed_accounts(["DU123"])
    await asyncio.sleep(0)

    contract = Contract(symbol="AAPL", secType="STK", exchange="SMART", currency="USD")
    client.emit_account_value("NetLiquidation", "15000", "USD", "DU123")
    client.emit_portfolio_update(contract, 10.0, 150.0, 1500.0, 1200.0, 300.0, 0.0, "DU123")
    client.emit_account_download_end("DU123")

    snapshot = await task
    assert snapshot["account"] == "DU123"
    assert snapshot["account_time"] is None
    assert client._account_updates_subscribed is False

    await controller.disconnect()


@pytest.mark.asyncio
async def test_request_account_updates_falls_back_without_managed_accounts() -> None:
    controller = IBAPIController(loop=asyncio.get_running_loop())

    await controller.connect()
    await controller.wait_until_connected()

    task = asyncio.create_task(controller.request_account_updates(timeout=1))
    await asyncio.sleep(0)

    client = controller.client
    contract = Contract(symbol="AAPL", secType="STK", exchange="SMART", currency="USD")
    client.emit_account_value("NetLiquidation", "15000", "USD", "DU123")
    client.emit_portfolio_update(contract, 10.0, 150.0, 1500.0, 1200.0, 300.0, 0.0, "DU123")
    client.emit_account_download_end("DU123")
    await asyncio.sleep(0)

    snapshot = await task
    assert snapshot["account"] == "DU123"
    assert client._account_updates_subscribed is False

    await controller.disconnect()


@pytest.mark.asyncio
async def test_request_account_updates_times_out_without_managed_accounts() -> None:
    controller = IBAPIController(loop=asyncio.get_running_loop())

    await controller.connect()
    await controller.wait_until_connected()

    with pytest.raises(asyncio.TimeoutError):
        await controller.request_account_updates(timeout=0.05)

    assert controller.client._account_updates_subscribed is False

    await controller.disconnect()


@pytest.mark.asyncio
async def test_request_account_updates_cancels_subscription_on_timeout() -> None:
    controller = IBAPIController(loop=asyncio.get_running_loop())

    await controller.connect()
    await controller.wait_until_connected()

    controller.client.emit_managed_accounts(["DU123"])
    await asyncio.sleep(0)

    with pytest.raises(asyncio.TimeoutError):
        await controller.request_account_updates(timeout=0.05)

    assert controller.client._account_updates_subscribed is False

    await controller.disconnect()


@pytest.mark.asyncio
async def test_request_positions_streams_positions() -> None:
    controller = IBAPIController(loop=asyncio.get_running_loop())

    await controller.connect()
    await controller.wait_until_connected()

    task = asyncio.create_task(controller.request_positions(timeout=1))
    await asyncio.sleep(0)

    contract = Contract(symbol="MES", secType="FUT", exchange="CME", currency="USD")
    controller.client.emit_position("DU123", contract, 2.0, 100.0)
    controller.client.emit_position("DU123", contract, 3.0, 100.0)
    controller.client.emit_position_end()

    positions = await task
    assert len(positions) == 2
    assert positions[0]["contract"].symbol == "MES"

    await controller.disconnect()
