from datetime import datetime
from typing import Any, Dict, List, Tuple

import pytest

from src.account_snapshot_service import AccountSnapshotService, IB_COMMAND_SOURCE
from src.ibbot.redis import IBCommandExecutionError, IBCommandTimeout


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeRedis:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, Dict[str, str]]] = []

    async def hset(self, key: str, mapping: Dict[str, Any]) -> int:
        self.calls.append((key, {str(k): str(v) for k, v in mapping.items()}))
        return 1

    async def close(self) -> None:
        pass

    async def ping(self) -> None:
        return None


class RecordingSnapshotRepository:
    def __init__(self) -> None:
        self.account_snapshots: List[Dict[str, Any]] = []
        self.account_sources: List[str] = []
        self.position_snapshots: List[Dict[str, Any]] = []
        self.position_sources: List[str] = []
        self.stale_reasons: List[str] = []

    async def write_account_info_snapshot(self, data: Dict[str, Any], source: str) -> bool:
        self.account_snapshots.append(dict(data))
        self.account_sources.append(source)
        return True

    async def write_positions_snapshot(self, data: Dict[str, Any], source: str) -> bool:
        self.position_snapshots.append(dict(data))
        self.position_sources.append(source)
        return True

    async def mark_all_snapshots_stale(self, reason: str) -> None:
        self.stale_reasons.append(reason)


async def test_refresh_account_info_success(monkeypatch: pytest.MonkeyPatch) -> None:
    service = AccountSnapshotService()
    fake_redis = FakeRedis()
    repository = RecordingSnapshotRepository()

    async def fake_get_client() -> FakeRedis:
        return fake_redis

    async def fake_request(method: str, **_: Any) -> Dict[str, Any]:
        assert method == "get_account_info"
        return {
            "status": "success",
            "command_id": "cmd-success",
            "result": {"balance": 123.45, "timestamp": datetime.now().isoformat()},
        }

    monkeypatch.setattr("src.account_snapshot_service.get_redis_client", fake_get_client)
    monkeypatch.setattr("src.account_snapshot_service.request_ib_command", fake_request)
    monkeypatch.setattr(
        "src.account_snapshot_service.write_account_info_snapshot",
        repository.write_account_info_snapshot,
    )

    result = await service.refresh_account_info(reason="unit-test")

    assert repository.account_snapshots, "Expected account snapshot to be persisted"
    stored_snapshot = repository.account_snapshots[-1]
    assert pytest.approx(stored_snapshot.get("balance", 0.0)) == 123.45
    assert repository.account_sources[-1] == IB_COMMAND_SOURCE
    assert result == stored_snapshot
    assert fake_redis.calls, "Expected health update call"
    key, mapping = fake_redis.calls[-1]
    assert key == "hash:service:health:account-snapshot"
    assert mapping["healthy"] == "true"
    assert mapping["command_id"] == "cmd-success"
    assert mapping["source"] == IB_COMMAND_SOURCE
    assert mapping["scope"] == "account_info"
    assert mapping["reason"] == "unit-test"


async def test_refresh_account_info_timeout_marks_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = AccountSnapshotService()
    fake_redis = FakeRedis()
    repository = RecordingSnapshotRepository()

    async def fake_get_client() -> FakeRedis:
        return fake_redis

    async def fake_request(method: str, **_: Any) -> Dict[str, Any]:
        raise IBCommandTimeout("timeout", command_id="cmd-timeout", payload={"method": method})

    monkeypatch.setattr("src.account_snapshot_service.get_redis_client", fake_get_client)
    monkeypatch.setattr("src.account_snapshot_service.request_ib_command", fake_request)
    monkeypatch.setattr(
        "src.account_snapshot_service.mark_all_snapshots_stale",
        repository.mark_all_snapshots_stale,
    )
    monkeypatch.setattr(
        "src.account_snapshot_service.write_account_info_snapshot",
        repository.write_account_info_snapshot,
    )

    result = await service.refresh_account_info(reason="unit-test")

    assert result is None
    assert repository.account_snapshots == []
    assert repository.stale_reasons == ["ib-command-timeout"]
    assert fake_redis.calls, "Expected health update call"
    _, mapping = fake_redis.calls[-1]
    assert mapping["healthy"] == "false"
    assert mapping["last_error"] == "timeout"
    assert mapping["command_id"] == "cmd-timeout"
    assert mapping["source"] == IB_COMMAND_SOURCE
    assert mapping["scope"] == "account_info"


async def test_refresh_positions_execution_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = AccountSnapshotService()
    fake_redis = FakeRedis()
    repository = RecordingSnapshotRepository()

    async def fake_get_client() -> FakeRedis:
        return fake_redis

    async def fake_request(method: str, **_: Any) -> Dict[str, Any]:
        raise IBCommandExecutionError(
            "failed",
            command_id="cmd-error",
            payload={"error": "boom", "method": method},
        )

    monkeypatch.setattr("src.account_snapshot_service.get_redis_client", fake_get_client)
    monkeypatch.setattr("src.account_snapshot_service.request_ib_command", fake_request)
    monkeypatch.setattr(
        "src.account_snapshot_service.mark_all_snapshots_stale",
        repository.mark_all_snapshots_stale,
    )
    monkeypatch.setattr(
        "src.account_snapshot_service.write_positions_snapshot",
        repository.write_positions_snapshot,
    )

    result = await service.refresh_positions(reason="unit-test", fast=True)

    assert result is None
    assert repository.position_snapshots == []
    assert repository.stale_reasons == []
    assert fake_redis.calls, "Expected health update call"
    _, mapping = fake_redis.calls[-1]
    assert mapping["healthy"] == "false"
    assert mapping["last_error"] == "boom"
    assert mapping["command_id"] == "cmd-error"
    assert mapping["source"] == IB_COMMAND_SOURCE
    assert mapping["scope"] == "positions"
