import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import orjson
import pytest

import scripts.refactor_integration_check as integration_check
from services.strategy.base import StrategyEventStream
from src.ib.events import (
    AccountUpdateEvent,
    DOMLevelEvent,
    ErrorEvent,
    ExecutionEvent,
    MarketTickEvent,
    OrderStatusEvent,
    PortfolioPositionEvent,
    RealTimeBarEvent,
    RedisChannel,
    TickByTickEvent,
)
from src.ib.redis_streaming import (
    RedisPublisher,
    publish_account_event,
    publish_dom_event,
    publish_error_event,
    publish_execution_event,
    publish_market_tick_event,
    publish_order_event,
    publish_portfolio_event,
    publish_real_time_bar_event,
    publish_tick_by_tick_event,
)
from tests.services.redis_utils import InMemoryRedis


@pytest.mark.asyncio
async def test_acceptance_event_flow_round_trip() -> None:
    redis_client = InMemoryRedis()
    publisher = RedisPublisher(redis=redis_client, service_name="acceptance")
    channels = [channel.value for channel in RedisChannel]
    stream = StrategyEventStream(
        redis_client,
        channels=channels,
        heartbeat_interval=0.2,
        heartbeat_timeout=1.0,
        queue_size=8,
    )

    timestamp = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)

    async def _publish_events() -> None:
        await publish_account_event(
            publisher,
            AccountUpdateEvent(
                account="DU12345",
                key="NetLiquidation",
                value="100000.25",
                currency="USD",
                timestamp=timestamp,
            ),
        )
        await publish_portfolio_event(
            publisher,
            PortfolioPositionEvent(
                account="DU12345",
                contract_id=265598,
                symbol="ES",
                security_type="FUT",
                position="2",
                average_cost="4300.5",
                market_price="4325.25",
                market_value="8650.5",
                realized_pnl="125.25",
                unrealized_pnl="350.75",
                currency="USD",
                timestamp=timestamp,
            ),
        )
        await publish_order_event(
            publisher,
            OrderStatusEvent(
                order_id=42,
                status="Filled",
                filled="2",
                remaining="0",
                avg_fill_price="4325.25",
                perm_id=9001,
                parent_id=None,
                last_fill_price="4325.25",
                client_id=7,
                why_held=None,
                timestamp=timestamp,
            ),
        )
        await publish_execution_event(
            publisher,
            ExecutionEvent(
                exec_id="exec-1",
                order_id=42,
                client_id=7,
                perm_id=9001,
                symbol="ES",
                security_type="FUT",
                exchange="GLOBEX",
                side="BOT",
                shares="2",
                price="4325.25",
                cumulative_quantity="2",
                average_price="4325.25",
                timestamp=timestamp,
            ),
        )
        await publish_market_tick_event(
            publisher,
            MarketTickEvent(
                contract_id=265598,
                tick_type="LAST",
                value="4325.25",
                timestamp=timestamp,
            ),
        )
        await publish_dom_event(
            publisher,
            DOMLevelEvent(
                contract_id=265598,
                position=0,
                operation="insert",
                side="ask",
                price="4325.50",
                size="3",
                timestamp=timestamp,
            ),
        )
        await publish_tick_by_tick_event(
            publisher,
            TickByTickEvent(
                contract_id=265598,
                tick_type="Last",
                price="4325.75",
                size="1",
                exchange="GLOBEX",
                special_conditions="",
                timestamp=timestamp,
            ),
        )
        await publish_real_time_bar_event(
            publisher,
            RealTimeBarEvent(
                contract_id=265598,
                open="4325.0",
                high="4326.0",
                low="4324.5",
                close="4325.5",
                volume="10",
                wap="4325.4",
                count=5,
                timestamp=timestamp,
            ),
        )
        await publish_error_event(
            publisher,
            ErrorEvent(
                request_id=101,
                code=1100,
                message="Connectivity lost",
                advanced_order_reject_json=None,
                timestamp=timestamp,
            ),
        )

    try:
        async with stream:
            publish_task = asyncio.create_task(_publish_events())
            await stream.wait_until_ready(timeout=1.0)
            await publish_task

            latest_by_channel = {channel: stream.latest_event(channel) for channel in channels}

        account_payload = latest_by_channel[RedisChannel.ACCOUNT_UPDATES.value]
        assert account_payload["account"] == "DU12345"
        assert account_payload["value"] == "100000.25"

        portfolio_payload = latest_by_channel[RedisChannel.PORTFOLIO.value]
        assert portfolio_payload["symbol"] == "ES"
        assert portfolio_payload["market_price"] == "4325.25"

        order_payload = latest_by_channel[RedisChannel.ORDERS.value]
        assert order_payload["status"] == "Filled"
        assert order_payload["filled"] == "2"

        execution_payload = latest_by_channel[RedisChannel.EXECUTIONS.value]
        assert execution_payload["side"] == "BOT"
        assert execution_payload["price"] == "4325.25"

        tick_payload = latest_by_channel[RedisChannel.MARKET_TICKS.value]
        assert tick_payload["tick_type"] == "LAST"
        assert tick_payload["value"] == "4325.25"

        dom_payload = latest_by_channel[RedisChannel.MARKET_DOM.value]
        assert dom_payload["operation"] == "insert"
        assert dom_payload["price"] == "4325.50"

        tbt_payload = latest_by_channel[RedisChannel.MARKET_TICK_BY_TICK.value]
        assert tbt_payload["exchange"] == "GLOBEX"
        assert tbt_payload["price"] == "4325.75"

        bar_payload = latest_by_channel[RedisChannel.MARKET_REAL_TIME_BARS.value]
        assert bar_payload["high"] == "4326.0"
        assert bar_payload["count"] == 5

        error_payload = latest_by_channel[RedisChannel.ERRORS.value]
        assert error_payload["code"] == 1100
        assert error_payload["message"] == "Connectivity lost"
    finally:
        await publisher.close()
        await redis_client.close()


@pytest.mark.asyncio
async def test_integration_capture_script_collects_payloads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    redis_client = InMemoryRedis()

    async def _publish_messages() -> None:
        await asyncio.sleep(0.05)
        await redis_client.publish(
            "ib:account:updates",
            orjson.dumps({"account": "DU12345", "key": "NetLiquidation", "value": "100000"}),
        )
        await redis_client.publish(
            "ib:orders:status",
            orjson.dumps({"order_id": 1, "status": "Submitted"}),
        )

    monkeypatch.setenv("REDIS_URL", "redis://acceptance")
    monkeypatch.setenv("ACCOUNT_SERVICE_URL", "http://account.test")
    monkeypatch.setenv("ORDERS_SERVICE_URL", "http://orders.test")
    monkeypatch.setenv("MARKET_DATA_SERVICE_URL", "http://market.test")
    monkeypatch.setenv("DEFAULT_ACCOUNT_CODE", "DU12345")

    monkeypatch.setattr(integration_check, "from_url", lambda url: redis_client)

    requests: list[tuple[str, str]] = []

    def _mock_handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, str(request.url)))
        if request.url.host == "account.test" and request.url.path == "/subscriptions/account":
            return httpx.Response(202, json={"status": "started"})
        if request.url.host == "orders.test" and request.url.path == "/subscriptions/orders":
            return httpx.Response(202, json={"status": "started"})
        if request.url.host == "market.test" and request.url.path == "/subscriptions/quotes":
            payload = orjson.loads(request.content) if request.content else {}
            body = {"status": "started", "contract": payload.get("contract")}
            return httpx.Response(202, json=body)
        if request.url.host == "market.test" and request.url.path == "/config":
            return httpx.Response(200, json={"channels": ["ib:account:updates", "ib:orders:status"]})
        return httpx.Response(404, json={"error": "unexpected"})

    transport = httpx.MockTransport(_mock_handler)
    real_async_client = httpx.AsyncClient

    def _mock_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.setdefault("transport", transport)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(integration_check.httpx, "AsyncClient", _mock_async_client)

    output_path = tmp_path / "capture.json"
    channels = ["ib:account:updates", "ib:orders:status"]

    publish_task = asyncio.create_task(_publish_messages())
    try:
        payload = await integration_check._run(0.2, output_path, channels)
        await publish_task
    finally:
        await redis_client.close()

    assert output_path.exists()
    assert any(path.endswith("/subscriptions/account") for _, path in requests)
    assert any(path.endswith("/subscriptions/orders") for _, path in requests)
    assert any(path.endswith("/subscriptions/quotes") for _, path in requests)
    assert any(path.endswith("/config") for _, path in requests)

    endpoint_bodies = [orjson.loads(item["body"]) for item in payload["endpoint_results"]]
    assert any(body.get("status") == "started" for body in endpoint_bodies)

    messages = payload["messages"]
    assert {msg["channel"] for msg in messages} == set(channels)
    assert payload["channels"] == channels

    stored = orjson.loads(output_path.read_bytes())
    assert stored["messages"] == messages
