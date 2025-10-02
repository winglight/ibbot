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
def service_status_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    app = FastAPI()
    app.include_router(service_module.router)

    original_build = service_status_module.build_service_status_payload

    async def fake_build(*_: object, **__: object):
        now = datetime.now(timezone.utc).isoformat()
        redis_data: dict[str, dict[str, str] | None] = {
            "hash:service:health:ib-command": {
                "last_seen": now,
                "queue_depth": "0",
            },
            "hash:service:queue-monitor": {"timestamp": now},
        }
        return await original_build(redis=StubRedis(redis_data))

    async def fake_readiness(**_: object) -> AccountServiceReadiness:
        return AccountServiceReadiness(ready=True, checked_at=datetime.now(timezone.utc), error=None)

    monkeypatch.setattr(service_module, "build_service_status_payload", fake_build)
    monkeypatch.setattr(service_status_module, "refresh_account_service_status", fake_readiness)

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

    for slug in ("ib_feed", "market_data", "optimizer"):
        entry = next((item for item in services if item.get("service") == slug), None)
        assert entry, f"missing service entry for {slug}"
        assert entry.get("status") == "unhealthy"
        assert "未上报" in (entry.get("detail") or "")
