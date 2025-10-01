import asyncio
import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.routes import orders as orders_module
from src.routes import strategy as strategy_module
from src.routes import market_data as market_data_module


@pytest.fixture()
def orders_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    app = FastAPI()
    app.include_router(orders_module.router)
    orders_module.set_websocket_broadcast_override(None)

    async def allow_user(request: Request) -> str:
        request.state.is_read_only = False
        request.state.user_role = "admin"
        request.state.username = "tester"
        return "tester"

    app.dependency_overrides[orders_module.get_current_user] = allow_user
    return app


def test_place_order_enqueues_command_and_returns_id(orders_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, Any] = {}

    async def fake_enqueue(payload: dict[str, Any]) -> None:
        recorded["payload"] = payload

    class DummyWebSocketManager:
        def __init__(self) -> None:
            self.messages: list[dict[str, Any]] = []

        async def broadcast(self, message: dict[str, Any]) -> None:
            self.messages.append(message)

    class FakeUUID:
        hex = "cmd-001"

    dummy_manager = DummyWebSocketManager()

    monkeypatch.setattr(orders_module, "_enqueue_command", fake_enqueue)
    orders_module.set_websocket_broadcast_override(dummy_manager.broadcast)
    monkeypatch.setattr(orders_module.uuid, "uuid4", lambda: FakeUUID())

    with TestClient(orders_app) as client:
        response = client.post(
            "/api/place_order",
            json={
                "symbol": "ES",
                "quantity": 2,
                "action": "BUY",
                "order_type": "market",
                "exchange": "CME",
                "sec_type": "FUT",
            },
        )

    assert response.status_code == 200
    payload = recorded["payload"]
    assert payload["command_id"] == "cmd-001"
    assert payload["method"] == "place_market_order"
    assert payload["kwargs"]["symbol"] == "ES"
    assert dummy_manager.messages and dummy_manager.messages[0]["command_id"] == "cmd-001"


def test_place_order_enqueue_failure_returns_service_unavailable(orders_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_enqueue(payload: dict[str, Any]) -> None:  # pragma: no cover - triggered in test
        raise RuntimeError("boom")

    monkeypatch.setattr(orders_module, "_enqueue_command", failing_enqueue)
    orders_module.set_websocket_broadcast_override(lambda message: None)

    with TestClient(orders_app) as client:
        response = client.post(
            "/api/place_order",
            json={
                "symbol": "NQ",
                "quantity": 1,
                "action": "SELL",
                "order_type": "market",
                "exchange": "CME",
                "sec_type": "FUT",
            },
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "Failed to enqueue order command"


def test_enqueue_command_uses_config_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, Any] = {}

    async def fake_enqueue(payload: dict[str, Any], *, queue_key: str) -> None:
        recorded["payload"] = dict(payload)
        recorded["queue_key"] = queue_key

    class DummyProvider:
        async def get(self, module: str, key: str, *, default: Any | None = None) -> str:
            assert module == "ib-command"
            assert key == "queue_key"
            return "queue:test:configured"

    class DummyContainer:
        config_provider = DummyProvider()

    monkeypatch.setattr(orders_module, "enqueue_ib_command", fake_enqueue)
    monkeypatch.setattr(orders_module.core, "get_container", lambda: DummyContainer())

    asyncio.run(orders_module._enqueue_command({"foo": "bar"}))

    assert recorded["payload"] == {"foo": "bar"}
    assert recorded["queue_key"] == "queue:test:configured"


@pytest.fixture()
def strategy_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    class DummyStrategyManager:
        def get_settings(self) -> dict[str, Any]:
            return {}

        def validate_auth_token(self, token: str) -> bool:
            return token == "valid"

        def get_strategy(self, name: str) -> dict[str, Any]:
            return {"enabled": True}

    monkeypatch.setattr(strategy_module, "strategy_manager", DummyStrategyManager())

    app = FastAPI()
    app.include_router(strategy_module.router)
    return app


def test_strategy_task_enqueued_and_event_returned(strategy_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeRedis:
        def __init__(self) -> None:
            self.queue: list[tuple[str, str]] = []
            self.events: list[tuple[str, dict[str, str]]] = [("1-0", {"task_id": "task-001", "status": "started"})]

        async def rpush(self, key: str, value: str) -> int:
            self.queue.append((key, value))
            return len(self.queue)

        async def xrevrange(self, key: str, count: int = 1) -> list[tuple[str, dict[str, str]]]:
            return list(self.events)

    fake_redis = FakeRedis()

    async def fake_get_redis_client() -> FakeRedis:
        return fake_redis

    class FakeUUID:
        hex = "task-001"

    monkeypatch.setattr(strategy_module, "get_redis_client", fake_get_redis_client)
    monkeypatch.setattr(strategy_module.uuid, "uuid4", lambda: FakeUUID())

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

    assert response.status_code == 200
    payload = response.json()
    assert payload["task_id"] == "task-001"
    assert payload["status"] == "started"
    assert payload["queue"] == strategy_module.STRATEGY_QUEUE_KEY
    assert payload["event"]["status"] == "started"

    assert fake_redis.queue, "expected payload to be pushed to Redis"
    queue_key, raw_value = fake_redis.queue[0]
    assert queue_key == strategy_module.STRATEGY_QUEUE_KEY
    body = json.loads(raw_value)
    assert body["task_id"] == "task-001"
    assert body["strategy"] == "demo"


def test_strategy_task_without_event_returns_queued(strategy_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeRedis:
        def __init__(self) -> None:
            self.queue: list[str] = []

        async def rpush(self, key: str, value: str) -> int:
            self.queue.append(value)
            return len(self.queue)

        async def xrevrange(self, key: str, count: int = 1) -> list[Any]:
            return []

    fake_redis = FakeRedis()

    async def fake_get_redis_client() -> FakeRedis:
        return fake_redis

    class FakeUUID:
        hex = "task-xyz"

    monkeypatch.setattr(strategy_module, "get_redis_client", fake_get_redis_client)
    monkeypatch.setattr(strategy_module.uuid, "uuid4", lambda: FakeUUID())

    with TestClient(strategy_app) as client:
        response = client.post(
            "/api/strategy",
            json={
                "strategy": "demo",
                "symbol": "CL",
                "action": "BUY",
                "qty": 1,
                "type": "market",
                "exchange": "NYMEX",
                "sec_type": "FUT",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "queued"
    assert "event" not in payload


@pytest.fixture()
def market_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    app = FastAPI()
    app.include_router(market_data_module.router)
    app.dependency_overrides[market_data_module.get_current_user] = lambda: "tester"
    return app


def test_market_data_route_returns_payload(market_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_market_get(path: str) -> httpx.Response:
        assert path == "/market/ES"
        return httpx.Response(200, json={"symbol": "ES", "price": 4300.5})

    monkeypatch.setattr(market_data_module, "_market_service_get", fake_market_get)

    with TestClient(market_app) as client:
        response = client.get("/api/realtime_data/ES")

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["data"]["symbol"] == "ES"


def test_market_data_route_handles_service_error(market_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_market_get(path: str) -> httpx.Response:
        return httpx.Response(503, text="down")

    monkeypatch.setattr(market_data_module, "_market_service_get", fake_market_get)

    with TestClient(market_app) as client:
        response = client.get("/api/realtime_data/NQ")

    assert response.status_code == 502
    assert "market data service" in response.json()["detail"].lower()
