from __future__ import annotations

import json
from typing import Any, Dict, Tuple

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.database import DatabaseManager
from src.routes import risk as risk_module


class FailingAsyncClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._base_url = kwargs.get("base_url", "http://example.com")

    async def __aenter__(self) -> "FailingAsyncClient":  # pragma: no cover - context protocol
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:  # pragma: no cover - context protocol
        return False

    async def request(self, method: str, url: str, **_: Any) -> Any:
        request = httpx.Request(method, f"{self._base_url}{url}")
        raise httpx.ConnectError("risk service offline", request=request)


class FakeRedis:
    def __init__(self) -> None:
        self.storage: Dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.storage.get(key)

    async def set(self, key: str, value: str) -> bool:
        self.storage[key] = value
        return True


@pytest.fixture()
def risk_app(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Tuple[FastAPI, DatabaseManager, FakeRedis]:
    app = FastAPI()
    app.include_router(risk_module.router)

    async def allow_user(request: Request) -> str:
        request.state.user_role = "admin"
        request.state.is_read_only = False
        request.state.username = "tester"
        return "tester"

    async def noop_audit(*_: Any, **__: Any) -> None:
        return None

    db_path = tmp_path / "risk.db"
    manager = DatabaseManager(str(db_path))
    fake_redis = FakeRedis()

    async def fake_get_redis() -> FakeRedis:
        return fake_redis

    monkeypatch.setattr(httpx, "AsyncClient", FailingAsyncClient)
    monkeypatch.setattr(risk_module, "RISK_SERVICE_URL", "http://example.com")
    monkeypatch.setattr(risk_module, "ENABLE_EMBEDDED_RISK_MANAGER", False)
    monkeypatch.setattr(risk_module, "get_db_manager", lambda: manager)
    monkeypatch.setattr(risk_module, "get_redis_client", fake_get_redis)
    monkeypatch.setattr(risk_module, "get_core_container", lambda request: (_ for _ in ()).throw(RuntimeError("no container")))
    monkeypatch.setattr(risk_module, "audit_log_success", noop_audit)
    monkeypatch.setattr(risk_module, "audit_log_failure", noop_audit)

    app.dependency_overrides[risk_module.get_current_user] = allow_user

    return app, manager, fake_redis


def _load_rules_from_cache(fake_redis: FakeRedis) -> Dict[str, Any]:
    raw = fake_redis.storage.get(risk_module.RISK_RULES_CACHE_KEY)
    return json.loads(raw) if raw else {}


def test_risk_rule_endpoints_use_fallback_when_service_unavailable(
    risk_app: Tuple[FastAPI, DatabaseManager, FakeRedis]
) -> None:
    app, db_manager, fake_redis = risk_app

    with TestClient(app) as client:
        response = client.post(
            "/api/risk_rules",
            json={
                "symbol": "ES",
                "stop_loss_price": 4100.0,
                "take_profit_price": 4300.0,
                "max_loss_percent": 0.05,
                "max_time_span": 60,
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is True
        assert payload["rule"]["symbol"] == "ES"

        update_resp = client.put(
            "/api/risk_rules/ES?enabled=false",
            json={
                "symbol": "ES",
                "stop_loss_price": 4050.0,
                "take_profit_price": 4200.0,
                "max_time_span": 90,
            },
        )
        assert update_resp.status_code == 200
        update_payload = update_resp.json()
        assert update_payload["success"] is True
        assert update_payload["rule"]["enabled"] is False

        rules_after_update = db_manager.load_risk_rules()
        assert len(rules_after_update) == 1
        assert rules_after_update[0].stop_loss_price == 4050.0
        assert rules_after_update[0].enabled is False

        interim_cache = _load_rules_from_cache(fake_redis)
        assert interim_cache.get("rules", {}).get("ES", {}).get("stop_loss_price") == 4050.0

        delete_resp = client.delete("/api/risk_rules/ES")
        assert delete_resp.status_code == 200
        delete_payload = delete_resp.json()
        assert delete_payload["success"] is True

    rules = db_manager.load_risk_rules()
    assert rules == []

    cache_snapshot = _load_rules_from_cache(fake_redis)
    assert cache_snapshot.get("success") is True
    assert cache_snapshot.get("count") == 0
