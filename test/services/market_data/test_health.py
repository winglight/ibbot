"""Health monitoring tests for the market data runtime."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

from services.market_data.runtime import MarketDataRuntime
from src.configuration.provider import MarketDataServiceConfig


class StubProvider:
    def __init__(self, config: MarketDataServiceConfig) -> None:
        self._config = config
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def get_config(self) -> MarketDataServiceConfig:
        return self._config

    def register_callback(self, callback: Any) -> None:  # pragma: no cover - unused in tests
        return None

    def unregister_callback(self, callback: Any) -> None:  # pragma: no cover - unused in tests
        return None


class StubListener:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


class RecordingRedis:
    def __init__(self) -> None:
        self.hset_calls: list[tuple[str, dict[str, Any]]] = []
        self.xadd_calls: list[tuple[str, dict[str, Any]]] = []
        self.hset_event = asyncio.Event()

    async def ping(self) -> bool:
        return True

    async def hset(self, key: str, *, mapping: dict[str, Any]) -> None:
        self.hset_calls.append((key, mapping))
        self.hset_event.set()

    async def xadd(
        self,
        key: str,
        fields: dict[str, Any],
        *,
        maxlen: int | None = None,
        approximate: bool = True,
    ) -> None:
        self.xadd_calls.append((key, fields))


class FailingRedis(RecordingRedis):
    def __init__(self, failures: int = 1) -> None:
        super().__init__()
        self.failures = failures

    async def xadd(
        self,
        key: str,
        fields: dict[str, Any],
        *,
        maxlen: int | None = None,
        approximate: bool = True,
    ) -> None:
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("stream failure")
        await super().xadd(key, fields, maxlen=maxlen, approximate=approximate)


@pytest.fixture(autouse=True)
def stub_snapshots(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_account_snapshot() -> dict[str, Any] | None:
        return {
            "data": {"balance": 0},
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    async def fake_positions_snapshot() -> dict[str, Any] | None:
        return {
            "data": [],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    monkeypatch.setattr(
        "services.market_data.runtime.read_account_info_snapshot",
        fake_account_snapshot,
    )
    monkeypatch.setattr(
        "services.market_data.runtime.read_positions_snapshot",
        fake_positions_snapshot,
    )


@pytest.fixture
def runtime_config() -> MarketDataServiceConfig:
    return MarketDataServiceConfig(
        symbols=["BTCUSDT"],
        stream_length=10,
        indicator_period=5,
        service_name="market-test",
        stream_key="stream:test",
        metrics_period=0.1,
    )


@pytest.mark.anyio("asyncio")
async def test_health_heartbeat_written(runtime_config: MarketDataServiceConfig) -> None:
    provider = StubProvider(runtime_config)
    listener = StubListener()
    redis = RecordingRedis()

    async def redis_factory() -> RecordingRedis:
        return redis

    async def provider_factory() -> StubProvider:
        return provider

    runtime = MarketDataRuntime(
        config_provider_factory=provider_factory,
        redis_factory=redis_factory,
        listener_factory=lambda config, runtime: listener,
        health_period=0.01,
    )

    await runtime.startup()
    await asyncio.wait_for(redis.hset_event.wait(), timeout=0.5)

    assert redis.hset_calls, "expected heartbeat writes"
    key, payload = redis.hset_calls[-1]
    assert key == "hash:service:health:market-data"
    assert payload["subscriptions"] == "0"
    assert payload["version"] == runtime.version
    assert "heartbeat_ts" in payload
    assert runtime.health_state["heartbeat_ts"] is not None

    await runtime.shutdown()


@pytest.mark.anyio("asyncio")
async def test_stream_error_updates_health(runtime_config: MarketDataServiceConfig) -> None:
    provider = StubProvider(runtime_config)
    listener = StubListener()
    redis = FailingRedis(failures=3)

    async def redis_factory() -> FailingRedis:
        return redis

    async def provider_factory() -> StubProvider:
        return provider

    runtime = MarketDataRuntime(
        config_provider_factory=provider_factory,
        redis_factory=redis_factory,
        listener_factory=lambda config, runtime: listener,
        health_period=0.0,
    )

    await runtime.startup()

    await runtime.publish_stream_event(
        symbol="BTCUSDT",
        event_type="price",
        data={"price": 123},
        message_timestamp=None,
        latency_ms=10.0,
    )

    assert runtime.health_state["last_stream_error"] is not None
    assert runtime.health_state["write_failures"]["redis_stream"] >= 1
    assert runtime.health_state["last_error"] is not None

    await runtime.shutdown()


@pytest.mark.anyio("asyncio")
async def test_latency_threshold_triggers_alert(runtime_config: MarketDataServiceConfig) -> None:
    provider = StubProvider(runtime_config)
    listener = StubListener()
    redis = RecordingRedis()
    alerts: list[tuple[str, dict[str, Any]]] = []

    async def redis_factory() -> RecordingRedis:
        return redis

    async def provider_factory() -> StubProvider:
        return provider

    def alert_callback(event_type: str, payload: dict[str, Any]) -> None:
        alerts.append((event_type, dict(payload)))

    runtime = MarketDataRuntime(
        config_provider_factory=provider_factory,
        redis_factory=redis_factory,
        listener_factory=lambda config, runtime: listener,
        latency_warning_ms=50.0,
        alert_callback=alert_callback,
        health_period=0.0,
    )

    await runtime.startup()

    await runtime.publish_stream_event(
        symbol="BTCUSDT",
        event_type="price",
        data={"price": 120},
        message_timestamp=None,
        latency_ms=120.0,
    )

    assert alerts, "expected latency alert"
    event_type, payload = alerts[-1]
    assert event_type == "latency_threshold"
    assert payload["symbol"] == "BTCUSDT"
    assert payload["threshold_ms"] == pytest.approx(50.0)
    assert runtime.health_state["last_latency_alert"] is not None

    await runtime.shutdown()

