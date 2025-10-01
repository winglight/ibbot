import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

import pytest

from services.market_data.runtime import MarketDataRuntime
from services.market_data.subscriptions import PubSubListener
from src.configuration.provider import MarketDataServiceConfig
from src.data_layer.event_bus import EventEnvelope
from src.data_layer.persistence import PersistenceBackend


pytestmark = pytest.mark.anyio("asyncio")


class FakeRedis:
    def __init__(self) -> None:
        self.stream_calls: list[tuple[str, dict[str, Any]]] = []
        self.hashes: dict[str, dict[str, Any]] = {}

    async def xadd(
        self,
        key: str,
        fields: dict[str, Any],
        *,
        maxlen: int | None = None,
        approximate: bool = True,
    ) -> None:  # noqa: ARG002
        self.stream_calls.append((key, fields))

    async def ping(self) -> bool:
        return True

    async def hset(self, key: str, mapping: dict[str, Any]) -> None:
        self.hashes[key] = mapping


class NoopListener:
    async def start(self) -> None:  # pragma: no cover - behaviour trivial
        return None

    async def stop(self) -> None:  # pragma: no cover - behaviour trivial
        return None


class StubProvider:
    def __init__(self, config: MarketDataServiceConfig) -> None:
        self._config = config

    async def start(self) -> None:  # pragma: no cover - unused
        return None

    async def stop(self) -> None:  # pragma: no cover - unused
        return None

    async def get_config(self) -> MarketDataServiceConfig:
        return self._config

    def register_callback(self, callback: Callable[[MarketDataServiceConfig], Awaitable[None] | None]) -> None:
        return None

    def unregister_callback(self, callback: Callable[[MarketDataServiceConfig], Awaitable[None] | None]) -> None:
        return None


class StubMetricsBackend(PersistenceBackend):
    def __init__(self) -> None:
        self.events: list[EventEnvelope] = []

    def write_event(self, envelope: EventEnvelope) -> None:
        self.events.append(envelope)


async def _idle_metrics_loop(runtime: MarketDataRuntime, backend: StubMetricsBackend) -> None:
    runtime.metrics_backend = backend
    await runtime._shutdown_event.wait()


async def test_high_frequency_stream_metrics() -> None:
    config = MarketDataServiceConfig(
        symbols=["BTCUSDT"],
        stream_length=128,
        metrics_period=1.0,
        service_name="market-data-test",
        stream_key="stream:market-test",
    )
    provider = StubProvider(config)
    redis = FakeRedis()
    backend = StubMetricsBackend()

    async def provider_factory() -> StubProvider:
        return provider

    async def redis_factory() -> FakeRedis:
        return redis

    async def listener_factory(
        cfg: MarketDataServiceConfig, runtime: MarketDataRuntime
    ) -> PubSubListener:  # noqa: ARG001
        return NoopListener()

    async def metrics_factory(runtime: MarketDataRuntime) -> None:
        await _idle_metrics_loop(runtime, backend)

    runtime = MarketDataRuntime(
        config_provider_factory=provider_factory,
        redis_factory=redis_factory,
        listener_factory=listener_factory,
        metrics_factory=metrics_factory,
        service_name="market-data-test",
        stream_key="stream:market-test",
        metrics_period=1.0,
    )

    await runtime.startup()
    assert runtime.stream_publisher is not None

    base_time = datetime.now(timezone.utc)
    latencies: list[float] = []
    for idx in range(50):
        latency = float((idx % 5) + 1)
        latencies.append(latency)
        message_timestamp = base_time - timedelta(milliseconds=latency)
        await runtime.publish_stream_event(
            symbol="BTCUSDT",
            event_type="price",
            data={"price": 100 + idx},
            message_timestamp=message_timestamp,
            latency_ms=latency,
        )

    assert len(redis.stream_calls) == 50

    await runtime._emit_metrics()

    assert backend.events, "metrics backend did not receive payload"
    event = backend.events[-1]
    assert event.payload["events"] == 50
    assert event.payload["interval_s"] == pytest.approx(1.0)
    assert event.payload["avg_latency_ms"] == pytest.approx(sum(latencies) / len(latencies))
    assert event.payload["max_latency_ms"] == pytest.approx(max(latencies))
    assert event.meta["latency_samples"] == 50
    assert event.meta["stream_key"] == "stream:market-test"
    assert event.meta["last_publish_ts_ns"] is not None

    metrics_summary = await runtime.stream_publisher.get_summary()
    assert metrics_summary["events"] == 0  # collect_metrics reset counters

    await runtime.shutdown()
