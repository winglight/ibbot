import json
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.routes import risk as risk_module
from src.routes.service_clients import ServiceUnavailableError


@pytest.fixture()
def risk_app(monkeypatch: pytest.MonkeyPatch):
    app = FastAPI()
    app.include_router(risk_module.router)

    async def allow_user(request: Request) -> str:
        request.state.is_read_only = False
        request.state.user_role = "admin"
        request.state.username = "tester"
        return "tester"

    app.dependency_overrides[risk_module.get_current_user] = allow_user

    for route in app.routes:
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        for dependency in dependant.dependencies:
            dep_call = getattr(dependency, "call", None)
            if dep_call is None:
                continue
            if dep_call is risk_module.get_current_user:
                app.dependency_overrides[dep_call] = allow_user
            elif getattr(dep_call, "__qualname__", "").startswith(
                "require_roles.<locals>.dependency"
            ):
                app.dependency_overrides[dep_call] = allow_user

    async def noop_audit(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(risk_module, "audit_log_success", noop_audit)
    monkeypatch.setattr(risk_module, "audit_log_failure", noop_audit)

    redis_store: dict[str, str | None] = {}

    class RedisStub:
        async def get(self, key: str) -> str | None:
            return redis_store.get(key)

    async def get_client():
        return RedisStub()

    monkeypatch.setattr(risk_module, "get_redis_client", get_client)

    class StubRiskServiceClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, Any]] = []
            self.responses: dict[str, Any] = {}
            self.exceptions: dict[str, Exception] = {}

        async def fetch_rules(self) -> Any:
            self.calls.append(("fetch_rules", None))
            exc = self.exceptions.get("fetch_rules")
            if exc:
                raise exc
            return self.responses.get("fetch_rules", {"success": True, "rules": []})

        async def fetch_config(self) -> Any:
            self.calls.append(("fetch_config", None))
            exc = self.exceptions.get("fetch_config")
            if exc:
                raise exc
            return self.responses.get("fetch_config", {"success": True, "config": {}})

        async def position_sizing(self, payload: dict[str, Any]) -> Any:
            self.calls.append(("position_sizing", dict(payload)))
            exc = self.exceptions.get("position_sizing")
            if exc:
                raise exc
            return self.responses.get(
                "position_sizing",
                {"success": True, "contracts": 1, "N": 1},
            )

    stub_client = StubRiskServiceClient()
    monkeypatch.setattr(risk_module, "_get_risk_service_client", lambda: stub_client)

    return app, stub_client, redis_store


def test_get_risk_rules_returns_cache_when_available(risk_app) -> None:
    app, stub_client, redis_store = risk_app
    redis_store[risk_module.RISK_RULES_CACHE_KEY] = json.dumps(
        {"success": True, "rules": [{"symbol": "ES"}], "source": "redis"}
    )

    with TestClient(app) as client:
        response = client.get("/api/risk_rules")

    assert response.status_code == 200
    payload = response.json()
    assert payload["rules"] == [{"symbol": "ES"}]
    assert payload["source"] == "redis"
    assert ("fetch_rules", None) not in stub_client.calls


def test_get_risk_rules_fetches_from_service_when_cache_empty(risk_app) -> None:
    app, stub_client, _ = risk_app
    stub_client.responses["fetch_rules"] = {"success": True, "rules": [{"symbol": "NQ"}]}

    with TestClient(app) as client:
        response = client.get("/api/risk_rules")

    assert response.status_code == 200
    payload = response.json()
    assert payload["rules"] == [{"symbol": "NQ"}]
    assert ("fetch_rules", None) in stub_client.calls


def test_get_risk_rules_returns_503_when_service_unavailable(risk_app) -> None:
    app, stub_client, _ = risk_app
    stub_client.exceptions["fetch_rules"] = ServiceUnavailableError("risk", "offline")

    with TestClient(app) as client:
        response = client.get("/api/risk_rules")

    assert response.status_code == 503
    assert response.json()["detail"] == "Risk service unavailable"


def test_get_risk_config_returns_cache_when_available(risk_app) -> None:
    app, stub_client, redis_store = risk_app
    redis_store[risk_module.RISK_CONFIG_CACHE_KEY] = json.dumps(
        {"success": True, "config": {"max": 5}, "source": "redis"}
    )

    with TestClient(app) as client:
        response = client.get("/api/risk-config")

    assert response.status_code == 200
    payload = response.json()
    assert payload["config"] == {"max": 5}
    assert payload["source"] == "redis"
    assert ("fetch_config", None) not in stub_client.calls


def test_get_risk_config_returns_503_when_service_unavailable_and_cache_empty(risk_app) -> None:
    app, stub_client, _ = risk_app
    stub_client.exceptions["fetch_config"] = ServiceUnavailableError("risk", "offline")

    with TestClient(app) as client:
        response = client.get("/api/risk-config")

    assert response.status_code == 503
    assert response.json()["detail"] == "Risk service unavailable"


def test_position_sizing_proxies_payload(risk_app) -> None:
    app, stub_client, _ = risk_app
    stub_client.responses["position_sizing"] = {
        "success": True,
        "contracts": 3,
        "N": 3,
        "detail": "ok",
    }

    request_payload = {
        "symbol": "ES",
        "stop_distance": 3.5,
        "rv_current": 1.2,
    }

    with TestClient(app) as client:
        response = client.post("/api/position-sizing", json=request_payload)

    assert response.status_code == 200
    assert response.json()["contracts"] == 3
    assert stub_client.calls[-1][0] == "position_sizing"
    assert stub_client.calls[-1][1]["symbol"] == "ES"


def test_position_sizing_returns_503_when_service_unavailable(risk_app) -> None:
    app, stub_client, _ = risk_app
    stub_client.exceptions["position_sizing"] = ServiceUnavailableError("risk", "offline")

    with TestClient(app) as client:
        response = client.post(
            "/api/position-sizing",
            json={"symbol": "ES", "stop_distance": 2.5, "rv_current": 1.0},
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "Risk service unavailable"
