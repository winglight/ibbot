import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import pytest

from services.risk import RiskRuntime


class RecordingRepository:
    def __init__(self) -> None:
        self.config_snapshots: list[dict[str, Any]] = []
        self.rule_snapshots: list[dict[str, Any]] = []

    async def save_config_snapshot(
        self,
        *,
        service: str,
        payload: Mapping[str, Any],
        rules_count: int,
        updated_at: str | None = None,
        captured_at: str | datetime | None = None,
    ) -> None:
        self.config_snapshots.append(
            {
                "service": service,
                "payload": dict(payload),
                "rules_count": rules_count,
                "updated_at": updated_at,
                "captured_at": captured_at,
            }
        )

    async def bulk_upsert_rule_snapshots(
        self,
        *,
        service: str,
        rules: Mapping[str, Mapping[str, Any]],
        updated_at: str | None = None,
        captured_at: str | datetime | None = None,
    ) -> None:
        self.rule_snapshots.append(
            {
                "service": service,
                "rules": {symbol: dict(payload) for symbol, payload in rules.items()},
                "updated_at": updated_at,
                "captured_at": captured_at,
            }
        )


class _InMemoryPipeline:
    def __init__(self, redis: "_InMemoryRedis") -> None:
        self._redis = redis
        self._operations: list[tuple[str, str, str]] = []

    def set(self, key: str, value: str) -> "_InMemoryPipeline":
        self._operations.append(("set", key, value))
        return self

    async def execute(self) -> list[bool]:
        results: list[bool] = []
        for op, key, value in self._operations:
            if op == "set":
                result = await self._redis.set(key, value)
                results.append(bool(result))
        self._operations.clear()
        return results


class _InMemoryRedis:
    def __init__(self) -> None:
        self._streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self._queues: dict[str, list[str]] = {}
        self._groups: dict[tuple[str, str], int] = {}
        self._counter = 0
        self._kv: dict[str, str] = {}

    async def flushall(self) -> None:
        self._streams.clear()
        self._queues.clear()
        self._groups.clear()
        self._counter = 0

    async def close(self) -> None:  # pragma: no cover - behaviour mirrors fakeredis
        return None

    async def xadd(self, key: str, fields: dict[str, Any]) -> str:
        self._counter += 1
        message_id = f"{self._counter}-0"
        stream = self._streams.setdefault(key, [])
        stream.append((message_id, {str(k): str(v) for k, v in fields.items()}))
        return message_id

    async def xgroup_create(self, stream: str, group: str, *, id: str = "$", mkstream: bool = False) -> None:
        if mkstream:
            self._streams.setdefault(stream, [])
        self._groups[(stream, group)] = len(self._streams.get(stream, []))

    async def xreadgroup(
        self,
        *,
        groupname: str,
        consumername: str,
        streams: dict[str, str],
        count: int | None = None,
        block: int | None = None,
    ) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        results: list[tuple[str, list[tuple[str, dict[str, str]]]]] = []
        for stream, _ in streams.items():
            entries = self._streams.get(stream, [])
            index = self._groups.get((stream, groupname), 0)
            if index >= len(entries):
                continue
            new_entries = entries[index:]
            if count is not None:
                new_entries = new_entries[:count]
            if not new_entries:
                continue
            self._groups[(stream, groupname)] = index + len(new_entries)
            results.append((stream, [(msg_id, dict(payload)) for msg_id, payload in new_entries]))
        if not results and block:
            await asyncio.sleep(block / 1000.0)
        return results

    async def xack(self, stream: str, group: str, *message_ids: str) -> int:
        return len(message_ids)

    async def rpush(self, key: str, value: str) -> int:
        queue = self._queues.setdefault(key, [])
        queue.append(value)
        return len(queue)

    async def blpop(self, key: str, timeout: float | int | None = 0) -> tuple[str, str] | None:
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        while True:
            queue = self._queues.setdefault(key, [])
            if queue:
                value = queue.pop(0)
                return key, value
            if deadline is not None and time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.01)

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self._kv[key] = value
        return True

    async def get(self, key: str) -> str | None:
        return self._kv.get(key)

    def pipeline(self) -> _InMemoryPipeline:
        return _InMemoryPipeline(self)


async def _wait_for(condition, *, timeout: float = 2.0, interval: float = 0.05):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        result = await condition()
        if result:
            return result
        await asyncio.sleep(interval)
    raise AssertionError("timeout waiting for condition")


def test_risk_runtime_triggers_stop_loss(tmp_path: Path) -> None:
    try:
        import fakeredis.aioredis as fakeredis  # type: ignore

        redis = fakeredis.FakeRedis(decode_responses=True)
    except ModuleNotFoundError:
        redis = _InMemoryRedis()
    asyncio.run(_run_integration(tmp_path, redis))


async def _run_integration(tmp_path: Path, redis: Any) -> None:
    await redis.flushall()

    repository = RecordingRepository()

    async def redis_factory():
        return redis

    runtime = RiskRuntime(
        redis_factory=redis_factory,
        risk_snapshot_repository=repository,
        logger_name="risk-service-test",
        service_name="risk-service-test",
        log_dir=tmp_path,
        poll_interval=0.05,
    )

    config = {
        "rules": json.dumps([{"symbol": "MNQ", "stop_loss": 100.0}]),
        "market_stream": "stream:market-service",
        "market_group": "group:risk:market",
        "results_stream": "stream:ib:results",
        "results_group": "group:risk:results",
        "command_queue": "queue:ib:commands",
    }
    await runtime.update_config(config)

    config_cache = await redis.get(runtime.config_cache_key)
    rules_cache = await redis.get(runtime.rules_cache_key)
    assert config_cache is not None
    assert rules_cache is not None

    config_snapshot = json.loads(config_cache)
    rules_snapshot = json.loads(rules_cache)
    assert isinstance(config_snapshot, dict)
    assert isinstance(rules_snapshot, dict)
    assert config_snapshot.get("updated_at")
    assert rules_snapshot.get("updated_at")
    assert rules_snapshot.get("success") is True
    assert rules_snapshot.get("rules")

    assert repository.config_snapshots
    latest_config = repository.config_snapshots[-1]
    assert latest_config["service"] == runtime.service_name
    assert latest_config["rules_count"] == 1
    assert int(latest_config["payload"].get("rules_count", 0)) == 1
    assert latest_config["payload"].get("updated_at")

    assert repository.rule_snapshots
    latest_rules = repository.rule_snapshots[-1]
    assert "MNQ" in latest_rules["rules"]

    await runtime.start()
    try:
        payload = json.dumps({"symbol": "MNQ", "last": 95.0})
        await redis.xadd("stream:market-service", {"symbol": "MNQ", "type": "price", "payload": payload})

        async def _pop_command():
            item = await redis.blpop("queue:ib:commands", timeout=1)
            if item is None:
                return None
            return json.loads(item[1])

        command = await _wait_for(_pop_command)
        assert command["kwargs"]["symbol"] == "MNQ"
        assert command["kwargs"]["reason"] == "risk_stop_loss"

        # Send a mock result message to release the trigger
        await redis.xadd(
            "stream:ib:results",
            {
                "command_id": command["command_id"],
                "status": "success",
            },
        )

        log_file = tmp_path / "risk-service-test.log"
        assert log_file.exists()
        log_content = log_file.read_text(encoding="utf-8")
        assert "Triggered stop_loss" in log_content
    finally:
        await runtime.stop()
        await redis.flushall()
        await redis.close()
