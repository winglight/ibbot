from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from src.service_status import gather_service_statuses, build_service_status_payload


class StubRedis:
    def __init__(self, mapping: dict[str, dict[str, str] | None]) -> None:
        self._mapping = mapping

    async def hgetall(self, key: str):  # type: ignore[override]
        await asyncio.sleep(0)
        value = self._mapping.get(key)
        if value is None:
            return {}
        return dict(value)


@pytest.fixture()
def service_status_patches(monkeypatch: pytest.MonkeyPatch):
    state = {
        "ready": True,
        "error": None,
        "checked_at": datetime.now(timezone.utc),
    }

    class DummyReadiness:
        def __init__(self, ready: bool, checked_at: datetime | None, error: str | None) -> None:
            self.ready = ready
            self.checked_at = checked_at
            self.error = error

        def as_payload(self) -> dict[str, object]:
            payload: dict[str, object] = {"ready": self.ready}
            if self.checked_at is not None:
                payload["checked_at"] = self.checked_at.isoformat()
            else:
                payload["checked_at"] = None
            if self.error:
                payload["error"] = self.error
            return payload

    async def fake_refresh_account_service_status(*_: object, **__: object) -> DummyReadiness:
        return DummyReadiness(state["ready"], state["checked_at"], state["error"])

    def fake_build_ib_payload(raw: dict[str, object], *, stale_after: float | None = None):
        _ = stale_after
        return {
            "connected": bool(raw.get("connected", True)),
            "queue_depth": int(raw.get("queue_depth", 0) or 0),
            "stale": bool(raw.get("stale", False)),
            "last_error": raw.get("last_error", ""),
            "last_seen": raw.get("last_seen"),
        }

    monkeypatch.setattr(
        "src.service_status.refresh_account_service_status",
        fake_refresh_account_service_status,
    )
    monkeypatch.setattr(
        "src.service_status.build_ib_connection_health_payload",
        fake_build_ib_payload,
    )

    return state


def test_gather_service_statuses_normalises_payloads(service_status_patches):
    async def _run() -> None:
        now = datetime.now(timezone.utc)
        recent = now.isoformat()
        redis_data = {
            "hash:service:health:ib-command": {
                "last_seen": recent,
                "queue_depth": "5",
                "last_error": "",
                "last_error_code": "",
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
            "hash:service:health:account-snapshot": None,
            "hash:service:health:queue": {
                "status": "running",
            },
            "hash:service:queue-monitor": {
                "timestamp": recent,
                "queue:ib-commands": "{}",
            },
        }

        statuses = await gather_service_statuses(redis=StubRedis(redis_data))
        status_map = {status.service: status for status in statuses}

        assert status_map["ibbot"].status == "running"
        assert status_map["risk-service"].status == "running"
        assert status_map["strategy"].status == "unhealthy"
        assert status_map["queue_monitor"].status == "running"
        assert status_map["ib-gateway"].status == "running"
        assert status_map["account-snapshot"].status == "unhealthy"

    asyncio.run(_run())


def test_build_service_status_payload_structure(service_status_patches):
    async def _run() -> None:
        now = datetime.now(timezone.utc).isoformat()
        redis_data = {
            "hash:service:health:ib-command": {
                "last_seen": now,
                "queue_depth": "0",
                "last_error": "",
                "last_error_code": "",
            },
            "hash:service:health:queue": {
                "status": "running",
                "last_seen": now,
                "total_queue_length": "0",
                "total_stream_pending": "0",
            },
            "hash:service:queue-monitor": {"timestamp": now},
        }

        payload = await build_service_status_payload(redis=StubRedis(redis_data))

        assert "generated_at" in payload
        services = payload.get("services")
        assert isinstance(services, list)
        assert any(entry.get("service") == "ibbot" for entry in services)
        # ensure metadata from registry is included
        ibbot_entry = next(entry for entry in services if entry.get("service") == "ibbot")
        assert ibbot_entry.get("label") == "Trading Core"
        assert ibbot_entry.get("internal") is True

    asyncio.run(_run())


def test_build_service_status_payload_includes_queue_metrics(service_status_patches):
    async def _run() -> None:
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
