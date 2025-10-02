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


class StubOrdersClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.should_fail = False

    async def list_orders(self, **_: Any) -> dict[str, Any]:
        return {"orders": []}

    async def place_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("place_order", dict(payload)))
        if self.should_fail:
            raise orders_module.ServiceUnavailableError("orders", "offline")
        return {"command_id": "cmd-001", "status": "accepted"}


@pytest.fixture()
def orders_app(monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, StubOrdersClient]:
    app = FastAPI()
    app.include_router(orders_module.router)
    orders_module.set_websocket_broadcast_override(None)

    client = StubOrdersClient()

    async def allow_user(request: Request) -> str:
        request.state.is_read_only = False
        request.state.user_role = "admin"
        request.state.username = "tester"
        return "tester"

    app.dependency_overrides[orders_module.get_current_user] = allow_user
    app.dependency_overrides[orders_module.get_orders_service_client] = lambda: client

    async def noop_audit(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(orders_module, "audit_log_success", noop_audit)
    monkeypatch.setattr(orders_module, "audit_log_failure", noop_audit)

    return app, client


def test_place_order_calls_orders_service(orders_app: tuple[FastAPI, StubOrdersClient]) -> None:
    app, client_stub = orders_app

    with TestClient(app) as client:
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
    payload = response.json()
    assert payload["command_id"] == "cmd-001"
    assert client_stub.calls and client_stub.calls[0][0] == "place_order"


def test_place_order_returns_503_when_service_offline(
    orders_app: tuple[FastAPI, StubOrdersClient]
) -> None:
    app, client_stub = orders_app
    client_stub.should_fail = True

    with TestClient(app) as client:
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
    assert response.json()["detail"] == "Orders service unavailable"


@pytest.fixture()
def strategy_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    class StubClient:
        async def request(self, method: str, path: str, **kwargs: Any) -> Any:
            raise strategy_module.ServiceUnavailableError("strategy", "offline")

    stub = StubClient()
    monkeypatch.setattr(strategy_module, "_STRATEGY_SERVICE_CLIENT", stub)
    monkeypatch.setattr(strategy_module, "_get_strategy_service_client", lambda: stub)

    app = FastAPI()
    app.include_router(strategy_module.router)

    async def allow_user() -> str:
        return "tester"

    app.dependency_overrides[strategy_module.get_current_user] = allow_user
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

    assert response.status_code == 503
    payload = response.json()
    assert payload["success"] is False
    assert payload["detail"] == "Strategy service unavailable; order queued"
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

    assert response.status_code == 503
    payload = response.json()
    assert payload["success"] is False
    assert payload["detail"] == "Strategy service unavailable; order queued"
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
