from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from src.account_service_readiness import AccountServiceReadiness
from src.routes.service_registry import SERVICE_REGISTRY
from src.service_status import gather_service_statuses, build_service_status_payload
from src.startup_checks import record_sqlite_handle_check, reset_startup_checks


class StubRedis:
    def __init__(self, mapping: dict[str, dict[str, str] | None]) -> None:
        self._mapping = mapping

    async def hgetall(self, key: str):  # type: ignore[override]
        await asyncio.sleep(0)
        value = self._mapping.get(key)
        if value is None:
            return {}
        return dict(value)


def test_gather_service_statuses_normalises_payloads(monkeypatch: pytest.MonkeyPatch):
    async def _run() -> None:
        reset_startup_checks()
        now = datetime.now(timezone.utc)
        recent = now.isoformat()
        async def _fake_readiness(**_: object) -> AccountServiceReadiness:
            return AccountServiceReadiness(ready=True, checked_at=now, error=None)

        monkeypatch.setattr(
            "src.service_status.refresh_account_service_status",
            _fake_readiness,
        )

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
                "stale": "false",
                "stale_reason": "",
            },
            "hash:service:health:orders": {
                "status": "running",
                "last_seen": recent,
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
            "hash:service:health:market-data": None,
            "hash:service:health:ib-feed": None,
            "hash:service:health:connection_control": None,
            "hash:service:health:notification": {
                "healthy": "true",
                "last_error": "",
                "last_seen": recent,
            },
            "hash:service:health:queue": {
                "status": "running",
                "total_queue_length": "2",
                "total_stream_pending": "1",
                "last_seen": recent,
            },
            "hash:service:queue-monitor": {
                "timestamp": recent,
                "queue:ib-commands": "{}",
            },
        }

        statuses = await gather_service_statuses(redis=StubRedis(redis_data))
        status_map = {status.service: status for status in statuses}

        assert status_map["ibbot"].status == "running"
        assert status_map["account"].status == "running"
        assert status_map["orders"].status == "running"
        assert status_map["risk"].status == "running"
        assert status_map["strategy"].status == "unhealthy"
        assert status_map["connection_control"].status == "unhealthy"
        assert status_map["ib_command"].status == "running"
        assert status_map["market_data_feed"].status == "unhealthy"
        assert status_map["market_data"].status == "unhealthy"
        assert "未上报" in (status_map["connection_control"].detail or "")
        assert "未上报" in (status_map["market_data_feed"].detail or "")
        assert "未上报" in (status_map["market_data"].detail or "")
        assert status_map["notification"].status == "running"
        assert status_map["notification"].payload is not None

        account_payload = status_map["account"].payload or {}
        assert "readiness" in account_payload
        metrics = account_payload.get("metrics") or {}
        assert metrics.get("snapshot_updated_at") == recent
        assert metrics.get("stale") is False
        assert "stale_reason" in metrics
        assert status_map["queue_monitor"].status == "running"
        queue_payload = status_map["queue_monitor"].payload or {}
        assert queue_payload.get("total_queue_length") == "2"
        assert "metrics_snapshot" in queue_payload

    asyncio.run(_run())


def test_account_service_status_falls_back_to_readiness(monkeypatch: pytest.MonkeyPatch):
    async def _run() -> None:
        reset_startup_checks()
        now = datetime.now(timezone.utc)

        async def _fake_readiness(**_: object) -> AccountServiceReadiness:
            return AccountServiceReadiness(ready=False, checked_at=now, error="boom")

        monkeypatch.setattr(
            "src.service_status.refresh_account_service_status",
            _fake_readiness,
        )

        redis_data = {
            "hash:service:health:ib-command": {
                "last_seen": now.isoformat(),
                "queue_depth": "0",
            },
            "hash:service:health:account-service": {
                "status": "running",
                "healthy": "true",
                "last_seen": now.isoformat(),
            },
            "hash:service:queue-monitor": {"timestamp": now.isoformat()},
        }

        statuses = await gather_service_statuses(redis=StubRedis(redis_data))
        status_map = {status.service: status for status in statuses}

        account_status = status_map["account"]
        assert account_status.status == "unhealthy"
        assert account_status.detail and "boom" in account_status.detail
        readiness_payload = (account_status.payload or {}).get("readiness")
        assert readiness_payload and readiness_payload.get("ready") is False

    asyncio.run(_run())


def test_build_service_status_payload_structure(monkeypatch: pytest.MonkeyPatch):
    async def _run() -> None:
        reset_startup_checks()
        now = datetime.now(timezone.utc).isoformat()

        async def _fake_readiness(**_: object) -> AccountServiceReadiness:
            return AccountServiceReadiness(ready=True, checked_at=datetime.now(timezone.utc), error=None)

        monkeypatch.setattr(
            "src.service_status.refresh_account_service_status",
            _fake_readiness,
        )

        redis_data = {
            "hash:service:health:ib-command": {
                "last_seen": now,
                "queue_depth": "0",
                "last_error": "",
                "last_error_code": "",
            },
            "hash:service:health:account-service": {
                "status": "running",
                "last_seen": now,
                "snapshot_updated_at": now,
                "stale": "false",
                "stale_reason": "",
            },
            "hash:service:health:orders": {
                "status": "running",
                "last_seen": now,
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
        assert any(entry.get("service") == "account" for entry in services)
        ordered_services = [entry.get("service") for entry in services]
        expected_order = [service.slug for service in SERVICE_REGISTRY]
        assert ordered_services[: len(expected_order)] == expected_order

        ibbot_entry = next(entry for entry in services if entry.get("service") == "ibbot")
        assert ibbot_entry.get("display_name") == "Trading Core"
        assert ibbot_entry.get("internal") is True
        assert ibbot_entry.get("health_key")
        assert ibbot_entry.get("implementation_status")

        market_entry = next(entry for entry in services if entry.get("service") == "market_data")
        assert market_entry.get("display_name") == "Market Data Sync"
        assert market_entry.get("status") == "unhealthy"
        assert "未上报" in (market_entry.get("detail") or "")

        connection_entry = next(entry for entry in services if entry.get("service") == "connection_control")
        assert connection_entry.get("display_name") == "Connection Control"
        assert connection_entry.get("internal") is True
        assert connection_entry.get("port") == "8056"

        account_entry = next(entry for entry in services if entry.get("service") == "account")
        account_metrics = (account_entry.get("payload") or {}).get("metrics") or {}
        assert account_metrics.get("snapshot_updated_at") == now
        assert account_metrics.get("stale") is False
        assert "stale_reason" in account_metrics
        assert connection_entry.get("default_health_key") == "hash:service:health:connection_control"
        assert connection_entry.get("health_key") == "hash:service:health:connection_control"
        assert connection_entry.get("status") == "unhealthy"
        assert "未上报" in (connection_entry.get("detail") or "")

    asyncio.run(_run())


def test_build_service_status_payload_includes_queue_metrics(monkeypatch: pytest.MonkeyPatch):
    async def _run() -> None:
        reset_startup_checks()
        now = datetime.now(timezone.utc).isoformat()
        async def _fake_readiness(**_: object) -> AccountServiceReadiness:
            return AccountServiceReadiness(ready=True, checked_at=datetime.now(timezone.utc), error=None)

        monkeypatch.setattr(
            "src.service_status.refresh_account_service_status",
            _fake_readiness,
        )

        redis_data = {
            "hash:service:health:ib-command": {
                "last_seen": now,
                "queue_depth": "2",
                "last_error": "",
                "last_error_code": "",
            },
            "hash:service:health:account-service": {
                "status": "running",
                "last_seen": now,
                "snapshot_updated_at": now,
                "stale": "false",
                "stale_reason": "",
            },
            "hash:service:health:orders": {
                "status": "running",
                "last_seen": now,
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
        assert any(
            "metrics_snapshot" in (entry.get("payload") or {})
            for entry in queue_entries
        )
        assert queue_entries[0].get("port") is None
        assert queue_entries[0].get("display_name") in {"Queue Monitor", "queue_monitor"}

    asyncio.run(_run())


def test_service_status_payload_includes_startup_checks(monkeypatch: pytest.MonkeyPatch):
    async def _run() -> None:
        reset_startup_checks()
        record_sqlite_handle_check([])

        async def _fake_readiness(**_: object) -> AccountServiceReadiness:
            return AccountServiceReadiness(ready=True, checked_at=datetime.now(timezone.utc), error=None)

        monkeypatch.setattr(
            "src.service_status.refresh_account_service_status",
            _fake_readiness,
        )

        now = datetime.now(timezone.utc).isoformat()
        redis_data = {
            "hash:service:health:ib-command": {
                "last_seen": now,
                "queue_depth": "0",
            },
            "hash:service:queue-monitor": {"timestamp": now},
        }

        payload = await build_service_status_payload(redis=StubRedis(redis_data))
        checks = payload.get("startup_checks")
        assert isinstance(checks, list)
        assert any(
            check.get("name") == "sqlite_handles" and check.get("healthy") is True
            for check in checks
        )

    asyncio.run(_run())
