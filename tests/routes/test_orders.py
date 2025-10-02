from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Mapping

import pytest
from fastapi import FastAPI, Request, status
from fastapi.testclient import TestClient

from src.routes import orders as orders_module


class StubOrdersClient:
    """Minimal stub for list-orders functionality."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, Any] | None]] = []

    async def list_orders(self, **_: Any) -> Mapping[str, Any]:
        self.calls.append(("list_orders", {}))
        return {"orders": [], "updated_at": None}


@pytest.fixture()
def orders_app(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[FastAPI, StubOrdersClient, list[tuple[str, Mapping[str, Any]]]]:
    app = FastAPI()
    app.include_router(orders_module.router)

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
    orders_module.set_websocket_broadcast_override(None)

    enqueued: list[tuple[str, Mapping[str, Any]]] = []

    async def fake_enqueue(command: Mapping[str, Any], *, queue_key: str) -> None:
        enqueued.append((queue_key, dict(command)))

    async def fake_queue_key() -> str:
        return "queue:test"

    command_ids = iter([
        "cmd-123",
        "cmd-cancel",
        "cmd-cancel-path",
        "cmd-all",
        "cmd-close",
        "cmd-reverse",
    ])

    def fake_uuid4() -> SimpleNamespace:
        return SimpleNamespace(hex=next(command_ids))

    monkeypatch.setattr(orders_module, "enqueue_ib_command", fake_enqueue)
    monkeypatch.setattr(orders_module, "_resolve_command_queue_key", fake_queue_key)
    monkeypatch.setattr(orders_module.uuid, "uuid4", fake_uuid4)

    return app, client, enqueued


def _order_payload() -> dict[str, Any]:
    return {
        "symbol": "ES",
        "quantity": 2,
        "action": "BUY",
        "order_type": "market",
        "exchange": "CME",
        "sec_type": "FUT",
    }


def test_place_order_success_returns_command_id(
    orders_app: tuple[FastAPI, StubOrdersClient, list[tuple[str, Mapping[str, Any]]]]
) -> None:
    app, _, enqueued = orders_app

    with TestClient(app) as client:
        response = client.post("/api/place_order", json=_order_payload())

    assert response.status_code == status.HTTP_200_OK
    payload = response.json()
    assert payload["success"] is True
    assert payload["command_id"] == "cmd-123"
    assert payload["status"] == "queued"
    assert enqueued and enqueued[0][0] == "queue:test"
    assert enqueued[0][1]["method"] == "place_order"


def test_place_order_returns_503_when_enqueue_fails(
    orders_app: tuple[FastAPI, StubOrdersClient, list[tuple[str, Mapping[str, Any]]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _, _ = orders_app

    async def failing_enqueue(*_: Any, **__: Any) -> None:
        raise RuntimeError("redis down")

    monkeypatch.setattr(orders_module, "enqueue_ib_command", failing_enqueue)

    with TestClient(app) as client:
        response = client.post("/api/place_order", json=_order_payload())

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert response.json()["detail"] == "Failed to enqueue order command"


def test_cancel_order_enqueues_command(
    orders_app: tuple[FastAPI, StubOrdersClient, list[tuple[str, Mapping[str, Any]]]]
) -> None:
    app, _, enqueued = orders_app

    with TestClient(app) as client:
        response = client.post("/api/cancel_order", json={"order_id": "123"})

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["command_id"] == enqueued[-1][1]["command_id"]
    assert enqueued[-1][1]["method"] == "cancel_order"
    assert enqueued[-1][1]["kwargs"]["order_id"] == "123"


def test_cancel_order_delete_enqueues_command(
    orders_app: tuple[FastAPI, StubOrdersClient, list[tuple[str, Mapping[str, Any]]]]
) -> None:
    app, _, enqueued = orders_app

    with TestClient(app) as client:
        response = client.delete("/api/cancel_order/456")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["command_id"] == enqueued[-1][1]["command_id"]
    assert enqueued[-1][1]["kwargs"]["order_id"] == "456"


def test_bulk_commands_enqueued(
    orders_app: tuple[FastAPI, StubOrdersClient, list[tuple[str, Mapping[str, Any]]]]
) -> None:
    app, _, enqueued = orders_app

    with TestClient(app) as client:
        cancel_all = client.delete("/api/cancel_all_orders")
        close_position = client.post("/api/close_position", json={"symbol": "ES"})
        reverse_position = client.post(
            "/api/reverse_position",
            json={"symbol": "NQ"},
        )

    assert cancel_all.status_code == status.HTTP_200_OK
    assert close_position.status_code == status.HTTP_200_OK
    assert reverse_position.status_code == status.HTTP_200_OK

    methods = [entry[1]["method"] for entry in enqueued[-3:]]
    assert methods == ["cancel_all_orders", "close_position", "reverse_position"]


def test_list_orders_uses_service_client(
    orders_app: tuple[FastAPI, StubOrdersClient, list[tuple[str, Mapping[str, Any]]]]
) -> None:
    app, client_stub, _ = orders_app

    with TestClient(app) as client:
        response = client.get("/api/orders")

    assert response.status_code == status.HTTP_200_OK
    assert client_stub.calls and client_stub.calls[-1][0] == "list_orders"
