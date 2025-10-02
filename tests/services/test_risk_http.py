import pytest
from fastapi.testclient import TestClient

from services.risk.http import create_app


class RedisStub:
    def __init__(self, mapping: dict[str, str | bytes | None]) -> None:
        self.mapping = mapping
        self.closed = False

    async def get(self, key: str) -> str | bytes | None:
        return self.mapping.get(key)

    async def close(self) -> None:
        self.closed = True


def test_risk_http_returns_rules_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "json:risk:rules": '{"rules": {"ES": {"symbol": "ES", "enabled": true}}}',
        "json:risk:config": '{"updated_at": "2024-01-01T00:00:00Z"}',
    }

    async def factory() -> RedisStub:
        return RedisStub(payload)

    app = create_app(redis_factory=factory)

    with TestClient(app) as client:
        response = client.get("/risk_rules")

    body = response.json()
    assert response.status_code == 200
    assert body["success"] is True
    assert isinstance(body.get("rules"), list)
    assert body["rules"][0]["symbol"] == "ES"


def test_risk_http_reports_degraded_state(monkeypatch: pytest.MonkeyPatch) -> None:
    async def factory() -> RedisStub:
        return RedisStub({})

    app = create_app(redis_factory=factory)

    with TestClient(app) as client:
        response = client.get("/risk-config")

    body = response.json()
    assert response.status_code == 200
    assert body["success"] is False
    assert "error" in body
