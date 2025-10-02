from typing import Any

import pytest
from fastapi import FastAPI, Request, HTTPException
from fastapi.testclient import TestClient

from src.routes import orders as orders_module


@pytest.fixture()
def orders_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    app = FastAPI()
    app.include_router(orders_module.router)

    async def allow_user(request: Request) -> str:
        request.state.is_read_only = False
        request.state.user_role = "admin"
        request.state.username = "tester"
        return "tester"

    app.dependency_overrides[orders_module.get_current_user] = allow_user

    async def noop_audit(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(orders_module, "audit_log_success", noop_audit)
    monkeypatch.setattr(orders_module, "audit_log_failure", noop_audit)
    async def queue_key() -> str:
        return "queue:test"

    monkeypatch.setattr(orders_module, "_resolve_command_queue_key", queue_key)
    orders_module.set_websocket_broadcast_override(None)
    return app


def _order_payload() -> dict[str, Any]:
    return {
        "symbol": "ES",
        "quantity": 2,
        "action": "BUY",
        "order_type": "market",
        "exchange": "CME",
        "sec_type": "FUT",
    }


def test_place_order_success_enqueues_and_broadcasts(
    orders_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: dict[str, Any] = {}

    async def fake_enqueue(payload: dict[str, Any]) -> None:
        recorded["payload"] = dict(payload)

    async def fake_health() -> dict[str, Any]:
        recorded["health_checked"] = True
        return {"connected": True, "error_code": None}

    events: list[dict[str, Any]] = []

    async def fake_broadcast(message: dict[str, Any]) -> None:
        events.append(dict(message))

    class FakeUUID:
        hex = "cmd-xyz"

    monkeypatch.setattr(orders_module, "_enqueue_command", fake_enqueue)
    monkeypatch.setattr(orders_module, "_ensure_ib_command_available", fake_health)
    orders_module.set_websocket_broadcast_override(fake_broadcast)
    monkeypatch.setattr(orders_module.uuid, "uuid4", lambda: FakeUUID())

    with TestClient(orders_app) as client:
        response = client.post("/api/place_order", json=_order_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["command_id"] == "cmd-xyz"
    assert recorded["payload"]["command_id"] == "cmd-xyz"
    assert recorded["payload"]["method"] == "place_market_order"
    assert events and events[0]["command_id"] == "cmd-xyz"
    assert recorded.get("health_checked") is True


def test_place_order_queue_failure_returns_503(
    orders_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing_enqueue(payload: dict[str, Any]) -> None:
        raise RuntimeError("queue down")

    async def fake_health() -> dict[str, Any]:
        return {"connected": True, "error_code": None}

    monkeypatch.setattr(orders_module, "_enqueue_command", failing_enqueue)
    monkeypatch.setattr(orders_module, "_ensure_ib_command_available", fake_health)
    orders_module.set_websocket_broadcast_override(lambda message: None)

    with TestClient(orders_app) as client:
        response = client.post("/api/place_order", json=_order_payload())

    assert response.status_code == 503
    assert response.json()["detail"] == "Failed to enqueue order command"


def test_place_order_returns_503_when_health_missing(
    orders_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_health() -> dict[str, Any]:
        raise HTTPException(status_code=503, detail="ib-command service unavailable")

    async def fake_enqueue(payload: dict[str, Any]) -> None:
        raise AssertionError("enqueue should not be called when health fails")

    monkeypatch.setattr(orders_module, "_ensure_ib_command_available", fake_health)
    monkeypatch.setattr(orders_module, "_enqueue_command", fake_enqueue)
    orders_module.set_websocket_broadcast_override(lambda message: None)

    with TestClient(orders_app) as client:
        response = client.post("/api/place_order", json=_order_payload())

    assert response.status_code == 503
    assert response.json()["detail"] == "ib-command service unavailable"
