from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from src.account_service_readiness import AccountServiceReadiness
from src.routes.service_registry import DOM_SERVICE_NAME
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


def test_gather_service_statuses_normalises_payloads(monkeypatch: pytest.MonkeyPatch):
    async def _run() -> None:
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
            "hash:service:health:notification": {
                "healthy": "true",
                "last_seen": recent,
            },
            "hash:service:health:account-snapshot": None,
            "hash:service:health:ib-feed": {
                "status": "running",
                "last_seen": recent,
            },
            "hash:service:health:market-data": {
                "status": "running",
                "last_seen": recent,
            },
            "hash:service:health:optimizer": None,
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
        assert status_map["queue_monitor"].status == "running"
        assert status_map["notification"].status == "running"
        assert status_map["ib-gateway"].status == "external"
        assert status_map["cloudflared"].status == "external"
        assert status_map["account_snapshot"].status == "unhealthy"
        assert status_map["ib_feed"].status == "running"
        assert status_map["market_data"].status == "running"
        assert status_map["optimizer"].status == "unhealthy"
        assert status_map[DOM_SERVICE_NAME].status == "offline"

        account_payload = status_map["account"].payload or {}
        assert "readiness" in account_payload
        queue_payload = status_map["queue_monitor"].payload or {}
        assert queue_payload.get("total_queue_length") == "2"
        assert "metrics_snapshot" in queue_payload

    asyncio.run(_run())


def test_account_service_status_falls_back_to_readiness(monkeypatch: pytest.MonkeyPatch):
    async def _run() -> None:
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
        registry = payload.get("registry")
        assert isinstance(registry, list)
        assert any(entry.get("service") == "ib_feed" for entry in registry)
        assert any(entry.get("health_key") for entry in registry if entry.get("service") == "ib_feed")

    asyncio.run(_run())


def test_build_service_status_payload_includes_queue_metrics(monkeypatch: pytest.MonkeyPatch):
    async def _run() -> None:
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

        registry = payload.get("registry")
        assert isinstance(registry, list)
        assert any(entry.get("service") == "optimizer" for entry in registry)

    asyncio.run(_run())


def test_missing_health_entries_marked_unhealthy(monkeypatch: pytest.MonkeyPatch):
    async def _run() -> None:
        now = datetime.now(timezone.utc)

        async def _fake_readiness(**_: object) -> AccountServiceReadiness:
            return AccountServiceReadiness(ready=True, checked_at=now, error=None)

        monkeypatch.setattr(
            "src.service_status.refresh_account_service_status",
            _fake_readiness,
        )

        redis_data = {
            "hash:service:health:ib-command": {
                "last_seen": now.isoformat(),
                "queue_depth": "1",
            },
        }

        statuses = await gather_service_statuses(redis=StubRedis(redis_data))
        status_map = {status.service: status for status in statuses}

        feed_status = status_map["ib_feed"]
        market_status = status_map["market_data"]
        optimizer_status = status_map["optimizer"]

        assert feed_status.status == "unhealthy"
        assert feed_status.detail and "行情订阅服务未上报健康数据" in feed_status.detail
        assert market_status.status == "unhealthy"
        assert market_status.detail and "行情落地服务未上报健康数据" in market_status.detail
        assert optimizer_status.status == "unhealthy"
        assert optimizer_status.detail and "优化器未上报健康数据" in optimizer_status.detail

    asyncio.run(_run())
