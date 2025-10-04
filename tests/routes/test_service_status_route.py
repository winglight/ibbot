from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.account_service_readiness import AccountServiceReadiness
from src.routes import service as service_module
from src.routes.service_registry import SERVICE_REGISTRY
from src import service_status as service_status_module
from src.ibbot.redis import RedisConnectionError
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


class HangingRedis(StubRedis):
    def __init__(
        self,
        mapping: dict[str, dict[str, str] | None],
        *,
        hang_keys: set[str],
    ) -> None:
        super().__init__(mapping)
        self._hang_keys = set(hang_keys)
        self.closed = False

    async def hgetall(self, key: str):  # type: ignore[override]
        if key in self._hang_keys:
            await asyncio.sleep(3600)
        return await super().hgetall(key)

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def service_status_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    app = FastAPI()
    app.include_router(service_module.router)

    original_build = service_status_module.build_service_status_payload
    captured: dict[str, str] = {}

    async def fake_build(*_: object, **__: object):
        now = datetime.now(timezone.utc).isoformat()
        captured["now"] = now
        redis_data: dict[str, dict[str, str] | None] = {
            "hash:service:health:ib-command": {
                "last_seen": now,
                "queue_depth": "0",
            },
            "hash:service:queue-monitor": {"timestamp": now},
            "hash:service:health:queue": {
                "status": "running",
                "last_seen": now,
                "total_queue_length": "1",
            },
            "hash:service:health:account-service": {
                "status": "running",
                "last_seen": now,
                "snapshot_updated_at": now,
                "stale": "false",
                "stale_reason": "",
            },
            "hash:service:health:market-data": {
                "status": "running",
                "last_seen": now,
                "price_updates": "5",
                "price_throughput": "1.2",
            },
            "hash:service:health:ib-feed": {
                "status": "running",
                "last_seen": now,
                "price_updates_total": "12",
                "price_updates_delta": "2",
            },
            "hash:service:health:notification": {
                "status": "running",
                "last_seen": now,
                "healthy": "true",
            },
        }
        return await original_build(redis=StubRedis(redis_data))

    async def fake_readiness(**_: object) -> AccountServiceReadiness:
        return AccountServiceReadiness(ready=True, checked_at=datetime.now(timezone.utc), error=None)

    monkeypatch.setattr(service_module, "build_service_status_payload", fake_build)
    monkeypatch.setattr(service_status_module, "refresh_account_service_status", fake_readiness)

    reset_startup_checks()
    record_sqlite_handle_check([])
    app.state.service_status_captured = captured
    return app


def test_service_status_endpoint_returns_registry_order(service_status_app: FastAPI) -> None:
    with TestClient(service_status_app) as client:
        response = client.get("/api/services/status")

    assert response.status_code == 200
    payload = response.json()
    services = payload.get("services") or []
    names = [entry.get("service") for entry in services]
    expected = [service.slug for service in SERVICE_REGISTRY]
    assert names[: len(expected)] == expected

    assert payload.get("restart_mode") in {"docker", "local"}
    endpoint = payload.get("restart_endpoint")
    assert isinstance(endpoint, str) and endpoint.startswith("/services/restart")

    connection_entry = next(
        (item for item in services if item.get("service") == "connection_control"),
        None,
    )
    assert connection_entry is not None
    assert connection_entry.get("port") == "8056"
    assert connection_entry.get("default_health_key") == "hash:service:health:connection_control"
    assert connection_entry.get("health_key") == "hash:service:health:connection_control"
    assert connection_entry.get("status") == "unhealthy"
    assert "未上报" in (connection_entry.get("detail") or "")

    market_entry = next(
        (item for item in services if item.get("service") == "market_data"),
        None,
    )
    assert market_entry is not None
    assert market_entry.get("status") == "running"
    assert (market_entry.get("payload") or {}).get("price_updates") == "5"

    feed_entry = next(
        (item for item in services if item.get("service") == "market_data_feed"),
        None,
    )
    assert feed_entry is not None
    assert feed_entry.get("status") == "running"
    assert (feed_entry.get("payload") or {}).get("price_updates_total") == "12"

    notification_entry = next(
        (item for item in services if item.get("service") == "notification"),
        None,
    )
    assert notification_entry is not None
    captured_state = getattr(service_status_app.state, "service_status_captured", {})
    assert notification_entry.get("port") == "8060"
    assert notification_entry.get("default_health_key") == "hash:service:health:notification"
    assert notification_entry.get("health_key") == "hash:service:health:notification"
    assert notification_entry.get("status") == "running"
    assert notification_entry.get("last_reported") == captured_state.get("now")

    startup_checks = payload.get("startup_checks") or []
    assert any(
        check.get("name") == "sqlite_handles" and check.get("healthy") is True
        for check in startup_checks
    )

    account_entry = next(
        (item for item in services if item.get("service") == "account"),
        None,
    )
    assert account_entry is not None
    account_metrics = (account_entry.get("payload") or {}).get("metrics") or {}
    assert account_metrics.get("snapshot_updated_at") == captured_state.get("now")
    assert account_metrics.get("stale") is False

    queue_entry = next(
        (item for item in services if item.get("service") == "queue_monitor"),
        None,
    )
    assert queue_entry is not None
    assert queue_entry.get("default_health_key") == "hash:service:health:queue"
    assert queue_entry.get("health_key") == "hash:service:health:queue"
    assert queue_entry.get("status") == "running"


def test_service_status_endpoint_returns_redis_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastAPI()

    async def fake_refresh(**_: object) -> None:
        return None

    async def fake_get_client(*_: object, **__: object):
        raise RedisConnectionError("unreachable")

    monkeypatch.setattr(service_module, "refresh_account_service_status", fake_refresh)
    monkeypatch.setattr(service_status_module, "get_redis_client", fake_get_client)

    app.include_router(service_module.router)

    with TestClient(app) as client:
        response = client.get("/api/services/status")

    assert response.status_code == 503
    assert response.json() == {
        "detail": {"error": "redis_unavailable", "message": "Redis 服务不可用"}
    }


def test_service_status_endpoint_degrades_when_redis_hangs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()

    now = datetime.now(timezone.utc).isoformat()
    redis_data: dict[str, dict[str, str] | None] = {
        "hash:service:health:ib-command": {"last_seen": now, "queue_depth": "0"},
        "hash:service:health:account-service": {"status": "running", "last_seen": now},
        "hash:service:health:market-data": {"status": "running", "last_seen": now},
        "hash:service:health:queue": {
            "status": "running",
            "last_seen": now,
            "total_queue_length": "1",
        },
        "hash:service:queue-monitor": {"timestamp": now},
    }

    hanging_redis = HangingRedis(
        redis_data,
        hang_keys={"hash:service:health:market-data"},
    )

    async def fake_get_client(*_: object, **__: object):
        return hanging_redis

    async def fake_readiness(**_: object) -> AccountServiceReadiness:
        return AccountServiceReadiness(ready=True, checked_at=datetime.now(timezone.utc), error=None)

    monkeypatch.setattr(service_status_module, "get_redis_client", fake_get_client)
    monkeypatch.setattr(service_status_module, "refresh_account_service_status", fake_readiness)
    monkeypatch.setattr(service_status_module, "_REDIS_COMMAND_TIMEOUT", 0.01)

    app.include_router(service_module.router)

    with TestClient(app) as client:
        response = client.get("/api/services/status")

    assert response.status_code == 200
    payload = response.json()
    services = payload.get("services") or []
    market_entry = next(
        (item for item in services if item.get("service") == "market_data"),
        None,
    )
    assert market_entry is not None
    assert market_entry.get("status") == "unhealthy"
    detail = market_entry.get("detail") or ""
    assert "Redis" in detail
