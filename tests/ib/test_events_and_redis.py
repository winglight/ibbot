from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import orjson
import pytest

from src.ib.events import (
    account_update_from_ib,
    market_tick_from_ib,
    portfolio_position_from_ib,
)
from src.ib.redis_streaming import (
    RedisPublisher,
    publish_account_event,
    publish_market_tick_event,
    publish_portfolio_event,
)


class DummyRedis:
    def __init__(self, *, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.attempts = 0
        self.messages: list[tuple[str, bytes]] = []

    async def publish(self, channel: str, message: bytes) -> None:
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise ConnectionError("forced failure")
        self.messages.append((channel, message))

    async def close(self) -> None:  # pragma: no cover - compatibility
        return None


@pytest.mark.asyncio
async def test_account_update_factory_normalizes_value() -> None:
    timestamp = datetime(2024, 1, 15, 12, 30, tzinfo=timezone.utc)
    event = account_update_from_ib(
        "DU12345",
        "NetLiquidation",
        "1234.56",
        "USD",
        timestamp,
    )
    assert event.value == Decimal("1234.56")
    assert event.timestamp == timestamp

    publisher = RedisPublisher(redis=DummyRedis(), service_name="tests")
    await publish_account_event(publisher, event)
    assert publisher._redis.messages[0][0] == "ib:account:updates"
    payload = orjson.loads(publisher._redis.messages[0][1])
    assert payload["value"] == "1234.56"
    assert payload["timestamp"] == timestamp.isoformat()


@pytest.mark.asyncio
async def test_portfolio_serialization_handles_decimals(monkeypatch: pytest.MonkeyPatch) -> None:
    async def immediate_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", immediate_sleep)

    redis = DummyRedis(fail_times=2)
    publisher = RedisPublisher(redis=redis, service_name="tests")
    event = portfolio_position_from_ib(
        account="DU12345",
        contract_id=1001,
        symbol="AAPL",
        security_type="STK",
        position="10",
        average_cost="135.50",
        market_price="138.75",
        market_value="1387.5",
        realized_pnl="5",
        unrealized_pnl="25",
        currency="USD",
        timestamp=datetime(2024, 1, 15, 12, 31, tzinfo=timezone.utc),
    )

    await publish_portfolio_event(publisher, event)
    assert redis.attempts == 3
    channel, message = redis.messages[0]
    assert channel == "ib:account:portfolio"
    payload = orjson.loads(message)
    assert payload["average_cost"] == "135.50"
    assert payload["market_price"] == "138.75"
    assert payload["timestamp"].endswith("+00:00")


@pytest.mark.asyncio
async def test_market_tick_nan_serializes_as_string() -> None:
    event = market_tick_from_ib(
        contract_id=9001,
        tick_type="LAST",
        value=float("nan"),
        timestamp=datetime(2024, 1, 15, 12, 32, tzinfo=timezone.utc),
    )
    publisher = RedisPublisher(redis=DummyRedis(), service_name="tests")
    await publish_market_tick_event(publisher, event)
    payload = orjson.loads(publisher._redis.messages[0][1])
    assert payload["value"] == "NaN"
    assert payload["timestamp"].endswith("+00:00")
