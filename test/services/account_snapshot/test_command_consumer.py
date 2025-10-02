from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any, Dict, List

import pytest

from ibbot.config import AccountSnapshotServiceConfig
from services.account_snapshot.commands import AccountCommandConsumer


class FakeRedis:
    def __init__(self) -> None:
        self.lists: Dict[str, List[str]] = defaultdict(list)
        self.store: Dict[str, str] = {}
        self.hashes: Dict[str, Dict[str, str]] = defaultdict(dict)
        self.streams: Dict[str, List[tuple[str, Dict[str, str]]]] = defaultdict(list)
        self._expiry: Dict[str, float] = {}
        self._id_counter: Dict[str, int] = defaultdict(int)

    async def ping(self) -> bool:  # pragma: no cover - compatibility shim
        return True

    async def close(self) -> None:  # pragma: no cover - compatibility shim
        return None

    async def rpush(self, key: str, *values: str) -> int:
        self.lists[key].extend(values)
        return len(self.lists[key])

    async def blpop(self, key: str, timeout: int = 0) -> Any:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout if timeout else None
        while True:
            if self.lists[key]:
                value = self.lists[key].pop(0)
                return key, value
            if deadline is not None and loop.time() >= deadline:
                return None
            await asyncio.sleep(0.01)

    async def llen(self, key: str) -> int:
        return len(self.lists[key])

    async def set(self, key: str, value: str) -> bool:
        self.store[key] = value
        return True

    async def get(self, key: str) -> Any:
        return self.store.get(key)

    async def expire(self, key: str, ttl: int) -> bool:
        loop = asyncio.get_running_loop()
        self._expiry[key] = loop.time() + ttl
        return True

    async def hset(self, key: str, mapping: Dict[str, Any]) -> int:
        hash_ref = self.hashes[key]
        for field, value in mapping.items():
            hash_ref[field] = value if isinstance(value, str) else json.dumps(value)
        return len(mapping)

    async def hgetall(self, key: str) -> Dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def xadd(
        self,
        key: str,
        fields: Dict[str, str],
        *,
        maxlen: int | None = None,
        approximate: bool = True,
    ) -> str:
        self._id_counter[key] += 1
        entry_id = f"0-{self._id_counter[key]}"
        self.streams[key].append((entry_id, dict(fields)))
        if maxlen and len(self.streams[key]) > maxlen:
            self.streams[key] = self.streams[key][-maxlen:]
        return entry_id


async def _wait_for(predicate, *, timeout: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise TimeoutError("condition not met within timeout")


@pytest.fixture()
def snapshot_config() -> AccountSnapshotServiceConfig:
    return AccountSnapshotServiceConfig(
        redis_url="redis://localhost/0",
        service_name="account-snapshot-service",
        refresh_interval_seconds=0,
        check_interval_seconds=1,
        request_timeout_seconds=5,
        command_timeout_seconds=0.1,
        command_queue_key="queue:ib:commands",
        command_result_prefix="hash:ib:result:",
        command_result_ttl_seconds=60,
        command_retry_attempts=3,
        control_queue_key="queue:account-snapshot:control",
        signal_fast_positions=True,
        snapshot_stream_key="stream:account-snapshot",
        snapshot_stream_maxlen=50,
        health_hash_key="hash:service:health:account-snapshot",
    )


@pytest.fixture()
def fake_redis() -> FakeRedis:
    return FakeRedis()


class StubProvider:
    def __init__(self, account_payload: Dict[str, Any]) -> None:
        self.account_payload = account_payload
        self.info_calls = 0
        self.position_calls = 0

    async def get_account_info(self, *, reason: str) -> Dict[str, Any]:
        self.info_calls += 1
        payload = dict(self.account_payload)
        payload["reason"] = reason
        return payload

    async def get_positions(self, *, fast: bool, reason: str) -> Any:  # pragma: no cover
        self.position_calls += 1
        return []


@pytest.mark.asyncio
async def test_consumer_processes_account_info_command(monkeypatch, snapshot_config, fake_redis):
    written_info: List[Dict[str, Any]] = []

    async def fake_write_account_info_snapshot(data: Dict[str, Any], source: str) -> bool:
        written_info.append({"data": data, "source": source})
        return True

    async def fake_write_positions_snapshot(data: Any, source: str) -> bool:  # pragma: no cover
        return True

    monkeypatch.setattr(
        "services.account_snapshot.commands.write_account_info_snapshot",
        fake_write_account_info_snapshot,
    )
    monkeypatch.setattr(
        "services.account_snapshot.commands.write_positions_snapshot",
        fake_write_positions_snapshot,
    )

    provider = StubProvider({"account_id": "DU123", "net_liquidation": 1000})

    async def redis_factory():
        return fake_redis

    consumer = AccountCommandConsumer(
        config=snapshot_config,
        provider=provider,
        redis_factory=redis_factory,
    )

    await consumer.start()
    try:
        command_id = "cmd-success"
        command_payload = {
            "command_id": command_id,
            "command": "get_account_info",
            "payload": {"reason": "unit-test"},
        }
        await fake_redis.rpush(
            snapshot_config.command_queue_key, json.dumps(command_payload, ensure_ascii=False)
        )

        result_key = f"{snapshot_config.command_result_prefix}{command_id}"
        await _wait_for(lambda: result_key in fake_redis.store)

        result = json.loads(fake_redis.store[result_key])
        assert result["status"] == "ok"
        assert result["data"]["account_id"] == "DU123"
        assert provider.info_calls == 1

        assert written_info and written_info[0]["data"]["reason"] == "unit-test"
        stream_entries = fake_redis.streams[snapshot_config.snapshot_stream_key]
        assert len(stream_entries) == 1
        _, fields = stream_entries[0]
        assert fields["command"] == "get_account_info"
        assert fields["status"] == "ok"
        health = fake_redis.hashes[snapshot_config.health_hash_key]
        assert health["status"] == "ok"
    finally:
        await consumer.stop()


@pytest.mark.asyncio
async def test_consumer_records_failure_after_timeouts(monkeypatch, snapshot_config, fake_redis):
    async def fake_write_account_info_snapshot(data: Dict[str, Any], source: str) -> bool:  # pragma: no cover
        raise AssertionError("should not persist on failure")

    async def fake_write_positions_snapshot(data: Any, source: str) -> bool:  # pragma: no cover
        raise AssertionError("positions not expected")

    monkeypatch.setattr(
        "services.account_snapshot.commands.write_account_info_snapshot",
        fake_write_account_info_snapshot,
    )
    monkeypatch.setattr(
        "services.account_snapshot.commands.write_positions_snapshot",
        fake_write_positions_snapshot,
    )

    class SlowProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def get_account_info(self, *, reason: str) -> Dict[str, Any]:
            self.calls += 1
            await asyncio.sleep(snapshot_config.command_timeout_seconds + 0.05)
            return {"account_id": "DU123"}

        async def get_positions(self, *, fast: bool, reason: str) -> Any:  # pragma: no cover
            return []

    provider = SlowProvider()

    async def redis_factory():
        return fake_redis

    consumer = AccountCommandConsumer(
        config=snapshot_config,
        provider=provider,
        redis_factory=redis_factory,
    )

    await consumer.start()
    try:
        command_id = "cmd-timeout"
        command_payload = {
            "command_id": command_id,
            "command": "get_account_info",
            "payload": {"reason": "timeout-test"},
        }
        await fake_redis.rpush(
            snapshot_config.command_queue_key, json.dumps(command_payload, ensure_ascii=False)
        )

        result_key = f"{snapshot_config.command_result_prefix}{command_id}"
        await _wait_for(lambda: result_key in fake_redis.store, timeout=2.0)

        result = json.loads(fake_redis.store[result_key])
        assert result["status"] == "error"
        assert "Timed out" in result.get("error", "") or result.get("error")
        assert provider.calls == snapshot_config.command_retry_attempts

        stream_entries = fake_redis.streams[snapshot_config.snapshot_stream_key]
        assert stream_entries and stream_entries[0][1]["status"] == "error"
        health = fake_redis.hashes[snapshot_config.health_hash_key]
        assert health["status"] == "error"
        assert health["last_error"]
    finally:
        await consumer.stop()
