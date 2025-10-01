import json
from typing import Any

import pytest

from services.market_data.streams import StreamPublisher


pytestmark = pytest.mark.anyio("asyncio")


class FakeRedis:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], int, bool]] = []

    async def xadd(
        self,
        key: str,
        fields: dict[str, Any],
        *,
        maxlen: int | None = None,
        approximate: bool = True,
    ) -> None:
        self.calls.append((key, fields, maxlen or 0, approximate))


class FailingRedis(FakeRedis):
    async def xadd(
        self,
        key: str,
        fields: dict[str, Any],
        *,
        maxlen: int | None = None,
        approximate: bool = True,
    ) -> None:
        raise RuntimeError("boom")


async def test_stream_publisher_respects_maxlen_and_fields() -> None:
    redis = FakeRedis()
    publisher = StreamPublisher(redis_client=redis, maxlen=123, approximate=True)

    payload = {"price": 101.5}
    await publisher.publish(
        symbol="BTCUSDT",
        event_type="price",
        data=payload,
        ts_ns=42,
        latency_ms=12.5,
    )

    assert len(redis.calls) == 1
    key, fields, maxlen, approximate = redis.calls[0]
    assert key == "stream:market-service"
    assert maxlen == 123
    assert approximate is True
    assert fields["symbol"] == "BTCUSDT"
    assert fields["type"] == "price"
    assert json.loads(fields["data"]) == payload
    assert fields["ts_ns"] == "42"
    assert pytest.approx(float(fields["latency_ms"])) == 12.5


async def test_collect_metrics_aggregates_throughput_and_latency() -> None:
    redis = FakeRedis()
    publisher = StreamPublisher(redis_client=redis)

    await publisher.publish(symbol="BTCUSDT", event_type="price", data={}, latency_ms=10.0)
    await publisher.publish(symbol="BTCUSDT", event_type="dom", data={}, latency_ms=None)
    metrics = await publisher.collect_metrics()

    assert metrics["events"] == 2
    assert metrics["latency_samples"] == 1
    assert metrics["avg_latency_ms"] == pytest.approx(10.0)
    assert metrics["max_latency_ms"] == pytest.approx(10.0)
    assert metrics["last_error"] is None

    # Counters reset after collection
    metrics_after = await publisher.collect_metrics()
    assert metrics_after["events"] == 0
    assert metrics_after["latency_samples"] == 0


async def test_publish_failure_records_error_and_invokes_callback() -> None:
    redis = FailingRedis()
    observed: list[str | None] = []

    publisher = StreamPublisher(redis_client=redis, error_callback=observed.append)

    result = await publisher.publish(symbol="ETHUSDT", event_type="price", data={}, latency_ms=1.0)
    assert result is False
    assert observed[-1] and "boom" in observed[-1]
    metrics = await publisher.collect_metrics()
    assert metrics["events"] == 0
    assert metrics["last_error"] and "boom" in metrics["last_error"]

