from __future__ import annotations

import json
from typing import Any, Mapping

import pytest
from fastapi import FastAPI, Request, status
from fastapi.testclient import TestClient

from src.routes import risk as risk_module
from src.routes.service_clients import ServiceUnavailableError


class RedisStub:
    def __init__(self) -> None:
        self.values: dict[str, str | None] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.published: list[tuple[str, str]] = []

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def hgetall(self, key: str) -> Mapping[str, str]:
        return dict(self.hashes.get(key, {}))

    async def hset(self, key: str, mapping: Mapping[str, str]) -> None:
        bucket = self.hashes.setdefault(key, {})
        bucket.update({str(k): str(v) for k, v in mapping.items()})

    async def hdel(self, key: str, *fields: str) -> None:
        bucket = self.hashes.get(key)
        if not bucket:
            return
        for field in fields:
            bucket.pop(field, None)

    async def publish(self, channel: str, payload: str) -> None:
        self.published.append((channel, payload))


class StubRiskServiceClient:
    DEFAULTS = {
        "fetch_rules": {"success": True, "rules": {}},
        "fetch_config": {"success": True, "config": {}},
        "fetch_order_queue_status": {"success": True, "pending": 0},
        "position_sizing": {"success": True, "contracts": 1, "N": 1},
        "fetch_sizing_history": {"success": True, "data": []},
        "fetch_config_rules": {"success": True, "rules": {}},
    }

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.responses: dict[str, Any] = {}
        self.exceptions: dict[str, Exception] = {}

    async def fetch_rules(self) -> Any:
        return await self._call("fetch_rules")

    async def fetch_config(self) -> Any:
        return await self._call("fetch_config")

    async def fetch_order_queue_status(self) -> Any:
        return await self._call("fetch_order_queue_status")

    async def position_sizing(self, payload: Mapping[str, Any]) -> Any:
        return await self._call("position_sizing", dict(payload))

    async def fetch_sizing_history(
        self,
        *,
        symbol: str | None = None,
        since: str | None = None,
        limit: int | None = None,
    ) -> Any:
        payload = {"symbol": symbol, "since": since, "limit": limit}
        return await self._call("fetch_sizing_history", payload)

    async def fetch_config_rules(self) -> Any:
        return await self._call("fetch_config_rules")

    async def _call(self, name: str, payload: Any = None) -> Any:
        self.calls.append((name, payload))
        exc = self.exceptions.get(name)
        if exc:
            raise exc
        if name in self.responses:
            return self.responses[name]
        return self.DEFAULTS.get(name, {"success": True})


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

    redis = RedisStub()

    async def get_client() -> RedisStub:
        return redis

    monkeypatch.setattr(risk_module, "get_redis_client", get_client)

    async def noop_audit(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(risk_module, "audit_log_success", noop_audit)
    monkeypatch.setattr(risk_module, "audit_log_failure", noop_audit)

    stub_client = StubRiskServiceClient()
    monkeypatch.setattr(risk_module, "_get_risk_service_client", lambda: stub_client)

    return app, stub_client, redis


def test_get_risk_rules_returns_cache_when_available(risk_app) -> None:
    app, stub_client, redis = risk_app
    redis.values[risk_module.RISK_RULES_CACHE_KEY] = json.dumps(
        {"success": True, "rules": {"ES": {"symbol": "ES"}}, "source": "redis"}
    )

    with TestClient(app) as client:
        response = client.get("/api/risk_rules")

    assert response.status_code == status.HTTP_200_OK
    payload = response.json()
    assert payload["rules"] == {"ES": {"symbol": "ES"}}
    assert ("fetch_rules", None) not in stub_client.calls


def test_get_risk_rules_fetches_from_service_when_cache_empty(risk_app) -> None:
    app, stub_client, _ = risk_app
    stub_client.responses["fetch_rules"] = {"success": True, "rules": {"NQ": {"symbol": "NQ"}}}

    with TestClient(app) as client:
        response = client.get("/api/risk_rules")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["rules"] == {"NQ": {"symbol": "NQ"}}
    assert ("fetch_rules", None) in stub_client.calls


def test_get_risk_config_uses_cache_when_present(risk_app) -> None:
    app, stub_client, redis = risk_app
    redis.values[risk_module.RISK_CONFIG_CACHE_KEY] = json.dumps(
        {"success": True, "config": {"max": 5}, "source": "redis"}
    )

    with TestClient(app) as client:
        response = client.get("/api/risk-config")

    assert response.status_code == status.HTTP_200_OK
    payload = response.json()
    assert payload["config"] == {"max": 5}
    assert ("fetch_config", None) not in stub_client.calls


def test_get_risk_config_returns_503_when_service_unavailable_and_cache_empty(risk_app) -> None:
    app, stub_client, _ = risk_app
    stub_client.exceptions["fetch_config"] = ServiceUnavailableError("risk", "offline")

    with TestClient(app) as client:
        response = client.get("/api/risk-config")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert response.json()["detail"] == "Risk service unavailable"


def test_add_risk_rule_updates_config_hash(risk_app) -> None:
    app, _, redis = risk_app
    payload = {"symbol": "ES", "rule_type": "fixed", "stop_loss_price": 4300.5}

    with TestClient(app) as client:
        response = client.post("/api/risk_rules", json=payload)

    assert response.status_code == status.HTTP_200_OK
    config = redis.hashes.get(risk_module.RISK_CONFIG_HASH_KEY, {})
    assert risk_module.RISK_RULES_FIELD in config
    rules = json.loads(config[risk_module.RISK_RULES_FIELD])
    assert rules and rules[0]["symbol"] == "ES"
    assert redis.published[-1] == (
        risk_module.RISK_CONFIG_UPDATE_CHANNEL,
        risk_module.RISK_CONFIG_MODULE,
    )


def test_update_risk_rule_merges_into_existing_rule(risk_app) -> None:
    app, _, redis = risk_app
    redis.hashes[risk_module.RISK_CONFIG_HASH_KEY] = {
        risk_module.RISK_RULES_FIELD: json.dumps([{"symbol": "ES", "rule_type": "fixed"}])
    }

    with TestClient(app) as client:
        response = client.put(
            "/api/risk_rules/ES",
            json={"symbol": "ES", "take_profit_price": 4500.0},
        )

    assert response.status_code == status.HTTP_200_OK
    rules = json.loads(redis.hashes[risk_module.RISK_CONFIG_HASH_KEY][risk_module.RISK_RULES_FIELD])
    assert rules[0]["take_profit_price"] == 4500.0


def test_delete_risk_rule_removes_entry(risk_app) -> None:
    app, _, redis = risk_app
    redis.hashes[risk_module.RISK_CONFIG_HASH_KEY] = {
        risk_module.RISK_RULES_FIELD: json.dumps(
            [{"symbol": "ES", "rule_type": "fixed"}, {"symbol": "NQ", "rule_type": "fixed"}]
        )
    }

    with TestClient(app) as client:
        response = client.delete("/api/risk_rules/ES")

    assert response.status_code == status.HTTP_200_OK
    rules = json.loads(redis.hashes[risk_module.RISK_CONFIG_HASH_KEY][risk_module.RISK_RULES_FIELD])
    assert {entry["symbol"] for entry in rules} == {"NQ"}


def test_update_risk_config_persists_fields(risk_app) -> None:
    app, _, redis = risk_app

    with TestClient(app) as client:
        response = client.put(
            "/api/risk-config",
            json={"risk_per_trade_r": 0.5, "rv_ref_method": "static"},
        )

    assert response.status_code == status.HTTP_200_OK
    mapping = redis.hashes[risk_module.RISK_CONFIG_HASH_KEY]
    assert mapping["risk_per_trade_r"] == "0.5"
    assert mapping["rv_ref_method"] == "static"


def test_update_config_rules_bulk_merges(risk_app) -> None:
    app, _, redis = risk_app
    redis.hashes[risk_module.RISK_CONFIG_HASH_KEY] = {
        risk_module.RISK_RULES_FIELD: json.dumps([{"symbol": "ES", "rule_type": "fixed"}])
    }

    with TestClient(app) as client:
        response = client.put(
            "/api/config/rules",
            json={
                "ES": {"rule_type": "fixed", "stop_loss_price": 4200.0},
                "NQ": {"rule_type": "trailing", "trail_ticks": 20},
            },
        )

    assert response.status_code == status.HTTP_200_OK
    rules = json.loads(redis.hashes[risk_module.RISK_CONFIG_HASH_KEY][risk_module.RISK_RULES_FIELD])
    by_symbol = {entry["symbol"]: entry for entry in rules}
    assert by_symbol["ES"]["stop_loss_price"] == 4200.0
    assert by_symbol["NQ"]["rule_type"] == "trailing"


def test_position_sizing_proxies_payload(risk_app) -> None:
    app, stub_client, _ = risk_app
    stub_client.responses["position_sizing"] = {
        "success": True,
        "contracts": 3,
        "N": 3,
    }

    with TestClient(app) as client:
        response = client.post(
            "/api/position-sizing",
            json={"symbol": "ES", "stop_distance": 3.5, "rv_current": 1.2},
        )

    assert response.status_code == status.HTTP_200_OK
    assert stub_client.calls[-1][0] == "position_sizing"


def test_position_sizing_returns_503_when_service_unavailable(risk_app) -> None:
    app, stub_client, _ = risk_app
    stub_client.exceptions["position_sizing"] = ServiceUnavailableError("risk", "offline")

    with TestClient(app) as client:
        response = client.post(
            "/api/position-sizing",
            json={"symbol": "ES", "stop_distance": 2.5, "rv_current": 1.0},
        )

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert response.json()["detail"] == "Risk service unavailable"
