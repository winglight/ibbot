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


def test_gather_service_statuses_normalises_payloads():
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
            "hash:service:health:risk": {
                "status": "running",
                "last_seen": recent,
            },
            "hash:service:health:strategy": {
                "healthy": "false",
                "last_error": "boom",
                "last_seen": recent,
            },
            "hash:service:health:orders": {
                "status": "running",
                "last_seen": recent,
            },
            "hash:service:health:market_data": {
                "status": "running",
                "last_seen": recent,
            },
            "hash:service:health:connection_control": {
                "status": "unhealthy",
                "last_error": "offline",
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
        assert status_map["risk"].status == "running"
        assert status_map["strategy"].status == "unhealthy"
        assert status_map["orders"].status == "running"
        assert status_map["market_data"].status == "running"
        assert status_map["connection_control"].status == "unhealthy"
        assert status_map["queue_monitor"].status == "running"
        assert status_map["ib-gateway"].status == "running"
        assert status_map["account-snapshot"].status == "unhealthy"

    asyncio.run(_run())


def test_build_service_status_payload_structure():
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

    asyncio.run(_run())


def test_build_service_status_payload_includes_queue_metrics():
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
