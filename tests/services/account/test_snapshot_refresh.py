from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from services.account.main import (
    ACCOUNT_SERVICE_NAME,
    _evaluate_snapshot_freshness,
    _snapshot_watchdog_loop,
    _state,
    app,
)
from src.utils import now_local


pytestmark = pytest.mark.anyio


class _RecorderRedis:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def hset(self, key: str, mapping: Dict[str, Any]) -> int:
        self.calls.append({"key": key, "mapping": dict(mapping)})
        return 1


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def reset_account_state() -> None:
    _state.redis = None
    _state.health_task = None
    _state.watchdog_task = None
    _state.watchdog_stop_event = None
    _state.snapshot_service = None
    _state.last_refresh_request = None
    _state.health_interval = 0
    _state.stale = False
    _state.last_refresh_error = None
    _state.snapshot_updated_at = None
    _state.stale_reason = None
    yield
    _state.redis = None
    _state.health_task = None
    _state.watchdog_task = None
    _state.watchdog_stop_event = None
    _state.snapshot_service = None
    _state.last_refresh_request = None
    _state.health_interval = 0
    _state.stale = False
    _state.last_refresh_error = None
    _state.snapshot_updated_at = None
    _state.stale_reason = None


async def test_startup_manages_snapshot_service_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    started: List[bool] = []
    stopped: List[bool] = []

    class _StubSnapshotService:
        refresh_interval_seconds = 0

        async def start(self) -> None:
            started.append(True)

        async def stop(self) -> None:
            stopped.append(True)

        def request_immediate_refresh(self, reason: str = "connection_restored") -> None:
            return None

    async def _fake_get_redis_client(*_: Any, **__: Any) -> _RecorderRedis:
        return _RecorderRedis()

    monkeypatch.setenv("ACCOUNT_SERVICE_HEALTH_INTERVAL_SECONDS", "0")
    monkeypatch.setattr("services.account.main.AccountSnapshotService", _StubSnapshotService)
    monkeypatch.setattr("services.account.main.get_redis_client", _fake_get_redis_client)

    with TestClient(app):
        pass

    assert started == [True]
    assert stopped == [True]


async def test_watchdog_triggers_refresh_when_snapshots_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refresh_reasons: List[str] = []
    recorder = _RecorderRedis()

    class _WatchdogSnapshotService:
        def __init__(self) -> None:
            self.refresh_interval_seconds = 30

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

        def request_immediate_refresh(self, reason: str = "connection_restored") -> None:
            refresh_reasons.append(reason)

    stale_timestamp = (now_local() - timedelta(minutes=20)).isoformat()
    info_snapshot = {"updated_at": stale_timestamp}
    positions_snapshot = {"updated_at": stale_timestamp}

    async def _fake_collect() -> tuple[Dict[str, Any], Dict[str, Any]]:
        return info_snapshot, positions_snapshot

    async def _update_health(
        redis: Any,
        service_name: str,
        *,
        healthy: Optional[bool] = None,
        last_error: Optional[str] = None,
        metrics: Optional[Dict[str, Any]] = None,
        logger: Any | None = None,
    ) -> None:
        await recorder.hset(
            f"hash:service:health:{service_name}",
            mapping={
                "healthy": healthy,
                "last_error": last_error,
                **(metrics or {}),
            },
        )

    service = _WatchdogSnapshotService()
    _state.snapshot_service = service
    _state.watchdog_stop_event = asyncio.Event()
    _state.redis = recorder

    monkeypatch.setattr("services.account.main._collect_snapshots", _fake_collect)
    monkeypatch.setattr("services.account.main.update_service_health", _update_health)

    task = asyncio.create_task(_snapshot_watchdog_loop())
    await asyncio.sleep(0)
    _state.watchdog_stop_event.set()
    await task

    assert refresh_reasons == ["watchdog-stale"]
    assert _state.stale is True
    assert _state.last_refresh_error
    assert recorder.calls, "Expected watchdog health update"
    latest_call = recorder.calls[-1]
    assert latest_call["key"] == f"hash:service:health:{ACCOUNT_SERVICE_NAME}"
    assert latest_call["mapping"]["last_error"]
    assert latest_call["mapping"]["stale"] is True


def test_evaluate_snapshot_freshness_detects_staleness() -> None:
    current = now_local()
    fresh_time = current - timedelta(minutes=1)
    stale_time = current - timedelta(hours=2)
    info_snapshot = {"updated_at": fresh_time.isoformat()}
    positions_snapshot = {"updated_at": stale_time.isoformat()}

    stale, reason = _evaluate_snapshot_freshness(
        info_snapshot,
        positions_snapshot,
        refresh_interval=300,
        now=current,
    )

    assert stale is True
    assert "account:positions" in (reason or "")


def test_health_metrics_include_snapshot_metadata() -> None:
    info_snapshot = {"updated_at": "2024-01-01T00:00:00+00:00"}
    positions_snapshot = {"updated_at": "2024-01-01T01:00:00+00:00"}
    _state.stale = True
    _state.last_refresh_error = "unit-test"
    _state.stale_reason = "unit-test"

    metrics = _state.as_metrics(info=info_snapshot, positions=positions_snapshot)

    assert metrics["account_snapshot_updated_at"] == info_snapshot["updated_at"]
    assert metrics["positions_snapshot_updated_at"] == positions_snapshot["updated_at"]
    assert metrics["snapshot_updated_at"] == positions_snapshot["updated_at"]
    assert metrics["stale"] is True
    assert metrics["last_refresh_error"] == "unit-test"
    assert metrics["stale_reason"] == "unit-test"
