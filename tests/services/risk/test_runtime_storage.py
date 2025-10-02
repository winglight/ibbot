from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

import pytest

from services.risk.runtime import RiskRuntime


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _FakePipeline:
    def __init__(self, redis: "_FakeRedis") -> None:
        self._redis = redis
        self._operations: list[tuple[str, str]] = []

    def set(self, key: str, value: str) -> "_FakePipeline":
        self._operations.append((key, value))
        return self

    async def execute(self) -> list[bool]:
        results: list[bool] = []
        for key, value in self._operations:
            results.append(bool(await self._redis.set(key, value)))
        self._operations.clear()
        return results


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.published: list[tuple[str, str]] = []
        self._counter = 0

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str) -> bool:
        self.store[key] = value
        return True

    def pipeline(self) -> _FakePipeline:
        return _FakePipeline(self)

    async def publish(self, channel: str, message: str) -> int:
        self.published.append((channel, message))
        return 1

    async def hset(self, key: str, mapping: Mapping[str, Any]) -> int:
        bucket = self.hashes.setdefault(key, {})
        for field, value in mapping.items():
            bucket[str(field)] = str(value)
        return len(mapping)

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def hdel(self, key: str, *fields: str) -> int:
        bucket = self.hashes.get(key)
        if not bucket:
            return 0
        removed = 0
        for field in fields:
            if field in bucket:
                del bucket[field]
                removed += 1
        return removed

    async def xadd(
        self,
        key: str,
        fields: Mapping[str, Any],
        *,
        maxlen: int | None = None,
        approximate: bool | None = None,
    ) -> str:
        stream = self.streams.setdefault(key, [])
        entry_id = f"{int(self._counter)}-0"
        self._counter += 1
        stream.append((entry_id, {str(k): str(v) for k, v in fields.items()}))
        if maxlen is not None and len(stream) > int(maxlen):
            stream[:] = stream[-int(maxlen) :]
        return entry_id

    async def xlen(self, key: str) -> int:
        return len(self.streams.get(key, []))

    async def xrevrange(self, key: str, *, count: int | None = None) -> list[tuple[str, Mapping[str, str]]]:
        stream = list(reversed(self.streams.get(key, [])))
        if count is not None:
            return stream[: int(count)]
        return stream


async def test_rule_lifecycle_persists_and_records_history() -> None:
    redis = _FakeRedis()

    async def factory() -> _FakeRedis:
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
    redis = _FakeRedis()

    async def factory() -> _FakeRedis:
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
    redis = _FakeRedis()

    async def factory() -> _FakeRedis:
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
