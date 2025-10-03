from __future__ import annotations

from datetime import datetime, timezone

import json

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

    created = await runtime.save_risk_rule(
        "ES",
        {
            "stop_loss": 5,
            "enabled": True,
            "metadata": {
                "rule_type": "trailing",
                "atr_params": {"multiplier_sl": 1.3, "multiplier_tp": 2.1},
            },
        },
    )
    assert created["symbol"] == "ES"
    assert created["stop_loss"] == 5
    assert created["rule_type"] == "trailing"
    assert created["atr_params"] == {"multiplier_sl": 1.3, "multiplier_tp": 2.1}
    assert "rule_type" not in (created.get("metadata") or {})

    _, rules_snapshot = await runtime.reload_from_storage()
    assert "ES" in rules_snapshot
    assert rules_snapshot["ES"]["stop_loss"] == 5
    assert rules_snapshot["ES"]["rule_type"] == "trailing"
    assert rules_snapshot["ES"]["atr_params"]["multiplier_tp"] == 2.1

    updated = await runtime.save_risk_rule("ES", {"take_profit": 8})
    assert updated["take_profit"] == 8
    assert updated["stop_loss"] == 5
    assert updated["rule_type"] == "trailing"

    removed = await runtime.delete_risk_rule("ES")
    assert removed is True
    _, rules_snapshot = await runtime.reload_from_storage()
    assert "ES" not in rules_snapshot

    history = await runtime.fetch_rules_history()
    assert "history" in history
    events = [item.get("event") for item in history["history"]]
    assert events[-1] == "delete"
    assert history["count"] >= 3

    metrics = runtime.health_snapshot()
    assert metrics["rules_history_events"] == await redis.xlen(runtime.rules_history_stream)


async def test_load_risk_rules_promotes_cached_metadata() -> None:
    redis = FakeRedis()

    async def factory() -> FakeRedis:
        return redis

    runtime = RiskRuntime(redis_factory=factory, write_fn=lambda _: 0)

    legacy_payload = {
        "symbol": "es",
        "stop_loss_price": 10.0,
        "metadata": {
            "rule_type": "trailing",
            "atr_params": {"multiplier_sl": 1.4, "multiplier_tp": 2.2},
        },
    }
    await redis.hset(
        runtime.rules_hash_key,
        mapping={"es": json.dumps(legacy_payload)},
    )

    rules = await runtime.load_risk_rules()
    assert "ES" in rules
    es_rule = rules["ES"]
    assert es_rule["rule_type"] == "trailing"
    assert es_rule["atr_params"] == {
        "multiplier_sl": 1.4,
        "multiplier_tp": 2.2,
    }
    assert "rule_type" not in (es_rule.get("metadata") or {})

    stored_mapping = redis.hashes[runtime.rules_hash_key]
    assert "ES" in stored_mapping
    assert "es" not in stored_mapping
    stored_payload = json.loads(stored_mapping["ES"])
    assert stored_payload["rule_type"] == "trailing"
    assert stored_payload["atr_params"] == {
        "multiplier_sl": 1.4,
        "multiplier_tp": 2.2,
    }
    assert "rule_type" not in (stored_payload.get("metadata") or {})


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
    assert history_all["history"][0]["symbol"]

    history_filtered = await runtime.fetch_sizing_history(symbol="ES", limit=10)
    assert history_filtered["count"] == 1
    assert history_filtered["history"][0]["symbol"] == "ES"

    metrics = runtime.health_snapshot()
    assert metrics["sizing_history_events"] == await redis.xlen(runtime.sizing_history_stream)
