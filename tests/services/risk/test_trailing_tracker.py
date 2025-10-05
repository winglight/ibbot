import json
from datetime import datetime, timedelta, timezone

import pytest

from services.risk.runtime import RiskRuntime, TrailingRuleTracker
from tests.services.risk.helpers import FakeRedis


pytestmark = pytest.mark.anyio


class DummyPubSub:
    async def start(self) -> None:  # pragma: no cover - interface compatibility
        return None

    async def stop(self) -> None:  # pragma: no cover - interface compatibility
        return None

    def register_callback(self, channel: str, callback) -> None:  # pragma: no cover
        return None

    def unregister_channel(self, channel: str) -> None:  # pragma: no cover
        return None


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_trailing_tracker_updates_cache_and_throttles(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = FakeRedis()

    async def factory() -> FakeRedis:
        return redis

    runtime = RiskRuntime(redis_factory=factory, trailing_tracker_factory=lambda _: None)

    await runtime.save_risk_config({"risk_per_trade_r": 0.01})
    await runtime.save_risk_rule(
        "ES",
        {
            "symbol": "ES",
            "rule_type": "trailing",
            "enabled": True,
            "tick_size": 0.25,
            "trail_stop_ticks": 20,
            "trail_take_profit_ticks": 40,
            "trail_min_interval": 5,
            "stop_loss_price": 4980.0,
            "take_profit_price": 5005.0,
            "position": 1,
        },
    )

    tracker = TrailingRuleTracker(
        runtime,
        redis_factory=factory,
        pubsub_listener=DummyPubSub(),
    )

    await tracker.refresh_from_rules(runtime.get_rules_snapshot())
    state = tracker._states["ES"]

    import services.risk.runtime as runtime_module  # type: ignore

    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    moments = iter(
        [
            base,
            base + timedelta(seconds=1),
            base + timedelta(seconds=10),
            base + timedelta(seconds=20),
        ]
    )

    def fake_now() -> datetime:
        try:
            return next(moments)
        except StopIteration:
            return base + timedelta(seconds=30)

    monkeypatch.setattr(runtime_module, "_utc_now", fake_now)

    calls: list[dict[str, object]] = []
    original_apply = RiskRuntime.apply_runtime_rule_update

    async def wrapped_apply(self: RiskRuntime, symbol: str, updates, *, timestamp=None):
        if self is runtime:
            calls.append(
                {
                    "symbol": symbol,
                    "updates": dict(updates),
                    "timestamp": timestamp,
                }
            )
        return await original_apply(self, symbol, updates, timestamp=timestamp)

    monkeypatch.setattr(RiskRuntime, "apply_runtime_rule_update", wrapped_apply)

    await tracker._process_symbol(state, 5000.0, {"last": 5000.0})

    assert len(calls) == 1
    first_updates = calls[0]["updates"]
    assert pytest.approx(first_updates["stop_loss"]) == 4995.0
    assert pytest.approx(first_updates["take_profit"]) == 5010.0

    stored_rule = json.loads(redis.hashes[runtime.rules_hash_key]["ES"])
    assert pytest.approx(stored_rule["stop_loss"]) == 4995.0
    assert pytest.approx(stored_rule["take_profit"]) == 5010.0

    cached_rules = json.loads(redis.store[runtime.rules_cache_key])
    assert cached_rules["rules"]["ES"]["stop_loss"] == pytest.approx(4995.0)

    config_cache = json.loads(redis.store[runtime.config_cache_key])
    assert config_cache["rules_count"] >= 1

    assert len(redis.published) == 1
    channel, payload = redis.published[0]
    assert channel == runtime.rules_update_channel
    broadcast = json.loads(payload)
    assert broadcast["rules"]["ES"]["take_profit"] == pytest.approx(5010.0)

    await tracker._process_symbol(state, 5000.5, {"last": 5000.5})
    assert len(calls) == 1
    assert len(redis.published) == 1

    await tracker._process_symbol(state, 5003.0, {"last": 5003.0})
    assert len(calls) == 2

    latest_rule = json.loads(redis.hashes[runtime.rules_hash_key]["ES"])
    assert latest_rule["stop_loss"] == pytest.approx(4998.0)

    assert len(redis.published) == 2
    _, second_payload = redis.published[-1]
    second_broadcast = json.loads(second_payload)
    assert second_broadcast["rules"]["ES"]["stop_loss"] == pytest.approx(4998.0)
