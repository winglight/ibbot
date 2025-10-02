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
            "hash:service:queue-monitor": {"timestamp": now},
        }

        payload = await build_service_status_payload(redis=StubRedis(redis_data))

        assert "generated_at" in payload
        services = payload.get("services")
        assert isinstance(services, list)
        assert any(entry.get("service") == "ibbot" for entry in services)

    asyncio.run(_run())
