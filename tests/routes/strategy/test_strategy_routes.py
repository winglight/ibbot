import json
from typing import Any

import pytest
from fastapi import FastAPI

from fastapi.testclient import TestClient

from src.routes import strategy as strategy_module


@pytest.fixture()
def strategy_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    app = FastAPI()
    app.include_router(strategy_module.router)

    async def allow_user() -> str:
        return "tester"

    app.dependency_overrides[strategy_module.get_current_user] = allow_user
    return app


def _patch_service_client(monkeypatch: pytest.MonkeyPatch, client: Any) -> None:
    monkeypatch.setattr(strategy_module, "_STRATEGY_SERVICE_CLIENT", client)
    monkeypatch.setattr(strategy_module, "_get_strategy_service_client", lambda: client)


def test_submit_external_strategy_order_success(
    strategy_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: dict[str, Any] = {}

    class StubClient:
        async def request(self, method: str, path: str, **kwargs: Any) -> Any:
            recorded.update({
                "method": method,
                "path": path,
                "kwargs": kwargs,
            })
            return {"success": True, "order_id": "ord-001"}

    _patch_service_client(monkeypatch, StubClient())

    payload = {
        "strategy": "demo",
        "symbol": "ES",
        "action": "BUY",
        "qty": 1,
        "type": "market",
        "exchange": "CME",
        "sec_type": "FUT",
    }

    with TestClient(strategy_app) as client:
        response = client.post("/api/strategy", json=payload)

    assert response.status_code == 200
    assert response.json() == {"success": True, "order_id": "ord-001"}
    assert recorded["method"] == "POST"
    assert recorded["path"] == "/strategy"
    assert recorded["kwargs"]["json"]["strategy"] == "demo"
    metadata = recorded["kwargs"]["json"].get("metadata")
    assert isinstance(metadata, dict)
    assert metadata.get("client")


def test_submit_external_strategy_order_service_unavailable(
    strategy_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    class UnavailableClient:
        async def request(self, method: str, path: str, **kwargs: Any) -> Any:
            raise strategy_module.ServiceUnavailableError("strategy", "offline")

    _patch_service_client(monkeypatch, UnavailableClient())

    class FakeRedis:
        def __init__(self) -> None:
            self.queue: list[tuple[str, str]] = []
            self.events: list[tuple[str, dict[str, str]]] = [
                ("1-0", {"task_id": "task-123", "status": "started"})
            ]

        async def rpush(self, key: str, value: str) -> int:
            self.queue.append((key, value))
            return len(self.queue)

        async def xrevrange(self, key: str, count: int = 1) -> list[tuple[str, dict[str, str]]]:
            return list(self.events)

    fake_redis = FakeRedis()

    async def fake_get_redis_client() -> FakeRedis:
        return fake_redis

    class FixedUUID:
        hex = "task-123"

    monkeypatch.setattr(strategy_module, "get_redis_client", fake_get_redis_client)
    monkeypatch.setattr(strategy_module.uuid, "uuid4", lambda: FixedUUID())

    with TestClient(strategy_app) as client:
        response = client.post(
            "/api/strategy",
            json={
                "strategy": "demo",
                "symbol": "ES",
                "action": "BUY",
                "qty": 2,
                "type": "market",
                "exchange": "CME",
                "sec_type": "FUT",
            },
        )

    assert response.status_code == 503
    payload = response.json()
    assert payload["success"] is False
    assert payload["detail"] == "Strategy service unavailable; order queued"
    assert payload["task_id"] == "task-123"
    assert payload["status"] == "started"
    assert fake_redis.queue
    queue_key, queued_payload = fake_redis.queue[0]
    assert queue_key == strategy_module.STRATEGY_QUEUE_KEY
    assert "strategy" in json.loads(queued_payload)


def test_submit_external_strategy_order_queue_unavailable(
    strategy_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    class UnavailableClient:
        async def request(self, method: str, path: str, **kwargs: Any) -> Any:
            raise strategy_module.ServiceUnavailableError("strategy", "offline")

    _patch_service_client(monkeypatch, UnavailableClient())

    async def failing_get_redis_client():
        raise RuntimeError("redis down")

    monkeypatch.setattr(strategy_module, "get_redis_client", failing_get_redis_client)

    with TestClient(strategy_app) as client:
        response = client.post(
            "/api/strategy",
            json={
                "strategy": "demo",
                "symbol": "ES",
                "action": "BUY",
                "qty": 1,
                "type": "market",
                "exchange": "CME",
                "sec_type": "FUT",
            },
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "Strategy queue unavailable"
