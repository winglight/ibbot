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
        self.rule_snapshots: list[dict[str, Any]] = []

    async def replace_rules(
        self,
        rules: Mapping[str, Mapping[str, Any]],
        *,
        timestamp: str | datetime | None = None,
    ) -> None:
        self.rule_snapshots.append(
            {
                "rules": {symbol: dict(payload) for symbol, payload in rules.items()},
                "timestamp": timestamp,
            }
        )


class RecordingConfigSnapshot:
    def __init__(self, module: str, values: Mapping[str, str], version: str | None = None) -> None:
        self.module = module
        self.values = dict(values)
        self.version = version


class RecordingConfigRepository:
    def __init__(self) -> None:
        self.modules: dict[str, dict[str, str]] = {}
        self.saved: list[tuple[str, dict[str, str]]] = []

    async def load_module(self, module: str) -> RecordingConfigSnapshot:
        values = dict(self.modules.get(module, {}))
        return RecordingConfigSnapshot(module, values, values.get("__version__"))

    async def save_module(
        self, module: str, values: Mapping[str, str]
    ) -> RecordingConfigSnapshot:
        prepared = {str(key): str(value) for key, value in values.items()}
        self.modules[module] = dict(prepared)
        self.saved.append((module, dict(prepared)))
        return RecordingConfigSnapshot(module, prepared, prepared.get("__version__"))


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
    config_repository = RecordingConfigRepository()

    async def redis_factory():
        return redis

    async def _repository_factory() -> Any:
        return repository

    async def _config_repository_factory() -> Any:
        return config_repository

    runtime = RiskRuntime(
        redis_factory=redis_factory,
        risk_rules_repository_factory=_repository_factory,
        config_repository_factory=_config_repository_factory,
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

    assert config_repository.saved
    latest_config_module, latest_payload = config_repository.saved[-1]
    assert latest_config_module == runtime.service_name
    stored_snapshot = json.loads(latest_payload["snapshot"])
    assert stored_snapshot.get("rules_count") == 1
    assert stored_snapshot.get("updated_at")

    assert repository.rule_snapshots
    latest_rules = repository.rule_snapshots[-1]
    assert "MNQ" in latest_rules["rules"]
    assert latest_rules.get("timestamp")

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


@pytest.mark.asyncio
async def test_config_persistence_across_restart(tmp_path: Path) -> None:
    redis_primary = _InMemoryRedis()
    redis_secondary = _InMemoryRedis()
    repository = RecordingRepository()
    config_repository = RecordingConfigRepository()

    async def _redis_primary() -> _InMemoryRedis:
        return redis_primary

    async def _redis_secondary() -> _InMemoryRedis:
        return redis_secondary

    async def _risk_repo_factory() -> RecordingRepository:
        return repository

    async def _config_repo_factory() -> RecordingConfigRepository:
        return config_repository

    runtime = RiskRuntime(
        redis_factory=_redis_primary,
        risk_rules_repository_factory=_risk_repo_factory,
        config_repository_factory=_config_repo_factory,
        logger_name="risk-service-test",
        service_name="risk-service-test",
        log_dir=tmp_path,
        poll_interval=0.05,
    )

    config = {
        "market_stream": "stream:market-service",
        "market_group": "group:risk:market",
        "results_stream": "stream:ib:results",
        "results_group": "group:risk:results",
        "command_queue": "queue:ib:commands",
        "rules": json.dumps([{"symbol": "ES", "stop_loss": 123.0}]),
    }

    await runtime.update_config(config)

    assert config_repository.saved

    runtime_reloaded = RiskRuntime(
        redis_factory=_redis_secondary,
        risk_rules_repository_factory=None,
        config_repository_factory=_config_repo_factory,
        logger_name="risk-service-test",
        service_name="risk-service-test",
        log_dir=tmp_path,
        poll_interval=0.05,
    )

    loaded_config = await runtime_reloaded.load_risk_config()
    assert loaded_config["market_stream"] == "stream:market-service"
    assert loaded_config["command_queue"] == "queue:ib:commands"
    assert loaded_config.get("rules_count") == 1
