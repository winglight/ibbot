from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from src.service_status import gather_service_statuses, build_service_status_payload


class StubRedis:
    def __init__(self, mapping: dict[str, dict[str, str] | None]) -> None:
        self._mapping = mapping

    async def hgetall(self, key: str):  # type: ignore[override]
        value = self._mapping.get(key)
        if value is None:
            return {}
        return dict(value)


class DummyReadiness:
    def __init__(self, *, ready: bool, checked_at: datetime | None, error: str | None = None):
        self.ready = ready
        self.checked_at = checked_at
        self.error = error

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"ready": self.ready}
        if self.checked_at is not None:
            payload["checked_at"] = self.checked_at.isoformat()
        if self.error:
            payload["error"] = self.error
        return payload


@pytest.fixture
def stub_account_readiness(monkeypatch):
    def _apply(*, ready: bool, error: str | None = None, checked_at: datetime | None = None) -> DummyReadiness:
        status = DummyReadiness(
            ready=ready,
            checked_at=checked_at or datetime.now(timezone.utc),
            error=error,
        )

        async def _fake_refresh(*, max_age: float | None = None, log_failure: bool = True):
            return status

        monkeypatch.setattr("src.service_status.refresh_account_service_status", _fake_refresh)
        return status

    return _apply


def test_gather_service_statuses_normalises_payloads(stub_account_readiness):
    now = datetime.now(timezone.utc)
    recent = now.isoformat()
    stub_account_readiness(ready=True, checked_at=now)

    redis_data = {
        "hash:service:health:ib-command": {
            "last_seen": recent,
            "queue_depth": "5",
            "last_error": "",
            "last_error_code": "",
        },
        "hash:service:health:account-service": {
            "status": "running",
            "last_seen": recent,
            "snapshot_updated_at": recent,
        },
        "hash:service:health:orders": {
            "healthy": "true",
            "last_seen": recent,
            "active_orders": "3",
        },
        "hash:service:health:risk-service": {
            "status": "running",
            "last_seen": recent,
        },
        "hash:service:health:strategy": {
            "healthy": "false",
            "last_error": "boom",
            "last_seen": recent,
        },
        "hash:service:health:notification": {
            "healthy": "true",
            "last_seen": recent,
        },
        "hash:service:health:account-snapshot": None,
        "hash:service:health:queue": {
            "status": "running",
            "last_seen": recent,
        },
        "hash:service:queue-monitor": {
            "timestamp": recent,
            "queue:ib-commands": "{}",
            "total_queue_length": "4",
        },
    }

    async def _run() -> None:
        statuses = await gather_service_statuses(redis=StubRedis(redis_data))
        status_map = {status.service: status for status in statuses}

        assert status_map["ibbot"].status == "running"
        assert status_map["account"].status == "running"
        assert status_map["orders"].status == "running"
        assert status_map["risk-service"].status == "running"
        assert status_map["strategy"].status == "unhealthy"
        assert status_map["notification"].status == "running"
        assert status_map["queue_monitor"].status == "running"
        assert status_map["ib-gateway"].payload == {"external": True}
        assert status_map["account-snapshot"].status == "unhealthy"

    asyncio.run(_run())


def test_build_service_status_payload_structure(stub_account_readiness):
    now = datetime.now(timezone.utc)
    stub_account_readiness(ready=False, error="offline", checked_at=now)
    iso_now = now.isoformat()

    redis_data = {
        "hash:service:health:ib-command": {
            "last_seen": iso_now,
            "queue_depth": "0",
            "last_error": "",
            "last_error_code": "",
        },
        "hash:service:health:account-service": {
            "healthy": "false",
            "last_error": "offline",
            "last_seen": iso_now,
        },
        "hash:service:health:queue": {
            "status": "running",
            "last_seen": iso_now,
            "total_queue_length": "0",
            "total_stream_pending": "0",
        },
        "hash:service:queue-monitor": {"timestamp": iso_now},
    }

    async def _run() -> None:
        payload = await build_service_status_payload(redis=StubRedis(redis_data))

        assert "generated_at" in payload
        services = payload.get("services")
        assert isinstance(services, list)
        assert any(entry.get("service") == "ibbot" for entry in services)
        assert any(entry.get("service") == "account" for entry in services)

    asyncio.run(_run())


def test_build_service_status_payload_includes_queue_metrics(stub_account_readiness):
    stub_account_readiness(ready=True, checked_at=datetime.now(timezone.utc))
    now = datetime.now(timezone.utc).isoformat()
    redis_data = {
        "hash:service:health:ib-command": {
            "last_seen": now,
            "queue_depth": "2",
            "last_error": "",
            "last_error_code": "",
        },
        "hash:service:health:queue": {
            "status": "running",
            "last_seen": now,
            "total_queue_length": "5",
            "total_stream_pending": "3",
        },
        "hash:service:queue-monitor": {
            "timestamp": now,
            "queue:ib-commands": "{}",
        },
    }

    async def _run() -> None:
        payload = await build_service_status_payload(redis=StubRedis(redis_data))
        services = payload.get("services")
        assert isinstance(services, list)

        queue_entries = [entry for entry in services if entry.get("service") == "queue_monitor"]
        assert queue_entries, "queue monitor service status should be present"
        assert any(
            entry.get("payload", {}).get("total_queue_length") == "5"
            and entry.get("payload", {}).get("total_stream_pending") == "3"
            for entry in queue_entries
        )

    asyncio.run(_run())
