from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Mapping

import pytest
from fastapi import FastAPI, Request, status
from fastapi.testclient import TestClient

from src.routes import orders as orders_module
from src.routes.service_clients import ServiceResponseError, ServiceUnavailableError


class StubOrdersClient:
    """Minimal stub for list-orders functionality."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, Any] | None]] = []
        self.get_order_response: Mapping[str, Any] | None = None
        self.get_order_error: Exception | None = None
        self.get_order_events_response: Mapping[str, Any] | None = None
        self.get_order_events_error: Exception | None = None

    async def list_orders(self, **kwargs: Any) -> Mapping[str, Any]:
        self.calls.append(("list_orders", dict(kwargs)))
        payload: dict[str, Any] = {"orders": [], "updated_at": None}
        start = kwargs.get("start_date")
        if start is not None:
            payload["start_date"] = start
        return payload

    async def get_order(self, order_id: str) -> Mapping[str, Any]:
        self.calls.append(("get_order", {"order_id": order_id}))
        if self.get_order_error:
            raise self.get_order_error
        if self.get_order_response is not None:
            return self.get_order_response
        return {"success": True, "order": {"id": order_id, "events": []}}

    async def get_order_events(self, order_id: str) -> Mapping[str, Any]:
        self.calls.append(("get_order_events", {"order_id": order_id}))
        if self.get_order_events_error:
            raise self.get_order_events_error
        if self.get_order_events_response is not None:
            return self.get_order_events_response
        return {"success": True, "order_id": order_id, "events": []}

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


def test_list_orders_normalises_start_date(
    orders_app: tuple[FastAPI, StubOrdersClient, list[tuple[str, Mapping[str, Any]]]]
) -> None:
    app, client_stub, _ = orders_app

    with TestClient(app) as client:
        response = client.get("/api/orders", params={"start_date": "2024-05-01"})

    assert response.status_code == status.HTTP_200_OK
    assert client_stub.calls and client_stub.calls[-1][0] == "list_orders"
    call_args = client_stub.calls[-1][1]
    assert isinstance(call_args, dict)
    start_arg = call_args.get("start_date")
    assert isinstance(start_arg, str)
    assert start_arg.startswith("2024-05-01T00:00:00")
    payload = response.json()
    assert payload.get("start_date", "").startswith("2024-05-01T00:00:00")
    meta = payload.get("meta")
    if isinstance(meta, dict):
        assert meta.get("start_date", "").startswith("2024-05-01T00:00:00")


def test_get_order_returns_payload(
    orders_app: tuple[FastAPI, StubOrdersClient, list[tuple[str, Mapping[str, Any]]]]
) -> None:
    app, client_stub, _ = orders_app
    client_stub.get_order_response = {
        "success": True,
        "order": {"id": "abc", "status": "pending", "events": []},
        "updated_at": "2024-06-01T00:00:00+00:00",
    }

    with TestClient(app) as client:
        response = client.get("/api/orders/abc")

    assert response.status_code == status.HTTP_200_OK
    payload = response.json()
    assert payload["success"] is True
    assert payload["order"]["id"] == "abc"
    assert client_stub.calls and client_stub.calls[-1][0] == "get_order"


def test_get_order_propagates_404(
    orders_app: tuple[FastAPI, StubOrdersClient, list[tuple[str, Mapping[str, Any]]]]
) -> None:
    app, client_stub, _ = orders_app
    client_stub.get_order_error = ServiceResponseError(
        "orders", status_code=404, detail="missing"
    )

    with TestClient(app) as client:
        response = client.get("/api/orders/missing")

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "missing"


def test_get_order_propagates_503(
    orders_app: tuple[FastAPI, StubOrdersClient, list[tuple[str, Mapping[str, Any]]]]
) -> None:
    app, client_stub, _ = orders_app
    client_stub.get_order_error = ServiceUnavailableError("orders", "down")

    with TestClient(app) as client:
        response = client.get("/api/orders/missing")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert response.json()["detail"] == "Orders service unavailable"


def test_get_order_events_returns_payload(
    orders_app: tuple[FastAPI, StubOrdersClient, list[tuple[str, Mapping[str, Any]]]]
) -> None:
    app, client_stub, _ = orders_app
    client_stub.get_order_events_response = {
        "success": True,
        "order_id": "abc",
        "events": [{"type": "update", "timestamp": "2024-06-01T00:00:00+00:00"}],
    }

    with TestClient(app) as client:
        response = client.get("/api/orders/abc/events")

    assert response.status_code == status.HTTP_200_OK
    payload = response.json()
    assert payload["success"] is True
    assert payload["order_id"] == "abc"
    assert payload["events"]


def test_get_order_events_propagates_404(
    orders_app: tuple[FastAPI, StubOrdersClient, list[tuple[str, Mapping[str, Any]]]]
) -> None:
    app, client_stub, _ = orders_app
    client_stub.get_order_events_error = ServiceResponseError(
        "orders", status_code=404, detail="not found"
    )

    with TestClient(app) as client:
        response = client.get("/api/orders/abc/events")

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "not found"
