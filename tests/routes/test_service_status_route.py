from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.routes import service as service_module


class DummyRedis:
    def __init__(self, mapping: dict[str, dict[str, str] | None]) -> None:
        self._mapping = mapping

    async def hgetall(self, key: str):  # type: ignore[override]
        value = self._mapping.get(key)
        if value is None:
            return {}
        return dict(value)

    async def close(self) -> None:  # pragma: no cover - compatibility shim
        return None


@pytest.fixture()
def services_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    app = FastAPI()
    app.include_router(service_module.router)

    now = datetime.now(timezone.utc).isoformat()
    redis_data: dict[str, dict[str, str] | None] = {
        "hash:service:health:ib-command": {
            "last_seen": now,
            "queue_depth": "1",
            "last_error": "",
        },
        "hash:service:health:risk-service": {
            "status": "running",
            "last_seen": now,
        },
        "hash:service:health:strategy": {
            "healthy": "false",
            "last_error": "boom",
            "last_seen": now,
        },
        "hash:service:health:queue": {
            "status": "running",
            "last_seen": now,
        },
        "hash:service:queue-monitor": {
            "timestamp": now,
            "queue:ib-commands": "{}",
        },
        # omit account snapshot health data to verify fallback behaviour
    }
    redis_stub = DummyRedis(redis_data)

    async def fake_get_redis_client() -> DummyRedis:
        return redis_stub

    monkeypatch.setattr("src.service_status.get_redis_client", fake_get_redis_client)

    class DummyReadiness:
        def __init__(self) -> None:
            self.ready = True
            self.checked_at = datetime.now(timezone.utc)
            self.error = None

        def as_payload(self) -> dict[str, object]:
            return {
                "ready": self.ready,
                "checked_at": self.checked_at.isoformat(),
            }

    async def fake_refresh_account_service_status(*_: object, **__: object) -> DummyReadiness:
        return DummyReadiness()

    def fake_build_ib_payload(raw: dict[str, object], *, stale_after: float | None = None):
        _ = stale_after
        return {
            "connected": True,
            "queue_depth": int(raw.get("queue_depth", 0) or 0),
            "stale": False,
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

    return app


def test_service_status_endpoint_includes_registry_entries(services_app: FastAPI) -> None:
    with TestClient(services_app) as client:
        response = client.get("/api/services/status")

    assert response.status_code == 200
    payload = response.json()
    services = payload.get("services", [])
    assert services, "service list should not be empty"

    service_map = {entry["service"]: entry for entry in services if "service" in entry}

    for name in ("market_data", "notification", "optimizer"):
        assert name in service_map
        assert service_map[name]["status"] == "running"
        assert service_map[name]["label"]
        assert service_map[name]["internal"] is True

    snapshot = service_map["account-snapshot"]
    assert snapshot["status"] == "unhealthy"
    assert "未上报" in snapshot.get("detail", "")

    # DOM service entry should adopt registry-provided label even without health data
    dom_entry = service_map.get("dom-service")
    if dom_entry is not None:
        assert dom_entry.get("label") == "DOM Bridge"
        assert dom_entry.get("internal") is False
