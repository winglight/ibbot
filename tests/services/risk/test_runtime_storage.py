from __future__ import annotations

from datetime import datetime, timezone

import pytest

from services.risk.runtime import RiskRuntime
from tests.services.risk.helpers import FakeRedis


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_rule_lifecycle_persists_and_records_history() -> None:
    redis = FakeRedis()

    async def factory() -> FakeRedis:
        return redis

    runtime = RiskRuntime(redis_factory=factory, write_fn=lambda _: 0)

    await runtime.save_risk_config({"risk_per_trade_r": 0.01})
    await runtime.reload_from_storage()

    created = await runtime.save_risk_rule("ES", {"stop_loss": 5, "enabled": True})
    assert created["symbol"] == "ES"
    assert created["stop_loss"] == 5

    _, rules_snapshot = await runtime.reload_from_storage()
    assert "ES" in rules_snapshot
    assert rules_snapshot["ES"]["stop_loss"] == 5

    updated = await runtime.save_risk_rule("ES", {"take_profit": 8})
    assert updated["take_profit"] == 8
    assert updated["stop_loss"] == 5

    removed = await runtime.delete_risk_rule("ES")
    assert removed is True
    _, rules_snapshot = await runtime.reload_from_storage()
    assert "ES" not in rules_snapshot

    history = await runtime.fetch_rules_history()
    events = [item.get("event") for item in history["items"]]
    assert events[-1] == "delete"
    assert history["count"] >= 3

    metrics = runtime.health_snapshot()
    assert metrics["rules_history_events"] == await redis.xlen(runtime.rules_history_stream)


async def test_config_saves_merge_and_removal() -> None:
    redis = FakeRedis()

    async def factory() -> FakeRedis:
        return redis

    runtime = RiskRuntime(redis_factory=factory, write_fn=lambda _: 0)

    await runtime.save_risk_config({"risk_per_trade_r": 0.02, "account_equity": 200000})
    config_snapshot, _ = await runtime.reload_from_storage()
    assert pytest.approx(config_snapshot["risk_per_trade_r"]) == 0.02
    assert config_snapshot["account_equity"] == 200000

    await runtime.save_risk_config({"account_equity": None, "risk_per_trade_r": 0.03})
    config_snapshot, _ = await runtime.reload_from_storage()
    assert pytest.approx(config_snapshot["risk_per_trade_r"]) == 0.03
    assert "account_equity" not in config_snapshot

    metrics = runtime.health_snapshot()
    assert metrics["config_snapshot_at"]


async def test_sizing_history_stream_filters_and_counts() -> None:
    redis = FakeRedis()

    async def factory() -> FakeRedis:
        return redis

    runtime = RiskRuntime(redis_factory=factory, write_fn=lambda _: 0)

    now = datetime.now(timezone.utc).isoformat()
    await runtime.append_sizing_history({"symbol": "ES", "contracts": 3, "timestamp": now})
    await runtime.append_sizing_history({"symbol": "NQ", "contracts": 2})

    history_all = await runtime.fetch_sizing_history(limit=10)
    assert history_all["count"] == 2

    history_filtered = await runtime.fetch_sizing_history(symbol="ES", limit=10)
    assert history_filtered["count"] == 1
    assert history_filtered["items"][0]["symbol"] == "ES"

    metrics = runtime.health_snapshot()
    assert metrics["sizing_history_events"] == await redis.xlen(runtime.sizing_history_stream)
