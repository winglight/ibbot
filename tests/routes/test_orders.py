from __future__ import annotations

from typing import Any, Mapping

import pytest
from fastapi import FastAPI, Request, status
from fastapi.testclient import TestClient

from src.routes import orders as orders_module


class StubOrdersClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, Any] | None]] = []
        self.place_order_response: Mapping[str, Any] | Exception = {
            "command_id": "cmd-123",
            "status": "accepted",
        }
        self.cancel_order_response: Mapping[str, Any] | Exception = {
            "command_id": "cmd-cancel",
            "status": "accepted",
        }

    async def list_orders(self, **_: Any) -> Mapping[str, Any]:
        return {"orders": [], "updated_at": None}

    async def place_order(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append(("place_order", payload))
        if isinstance(self.place_order_response, Exception):
            raise self.place_order_response
        return self.place_order_response

    async def cancel_order(self, order_id: str) -> Mapping[str, Any]:
        self.calls.append(("cancel_order", {"order_id": order_id}))
        if isinstance(self.cancel_order_response, Exception):
            raise self.cancel_order_response
        return self.cancel_order_response

    async def cancel_all(self) -> Mapping[str, Any]:
        self.calls.append(("cancel_all", {}))
        return {"command_id": "cmd-all", "status": "accepted"}

    async def close_position(self, symbol: str) -> Mapping[str, Any]:
        self.calls.append(("close_position", {"symbol": symbol}))
        return {"command_id": "cmd-close", "status": "accepted"}

    async def reverse_position(self, symbol: str) -> Mapping[str, Any]:
        self.calls.append(("reverse_position", {"symbol": symbol}))
        return {"command_id": "cmd-reverse", "status": "accepted"}


@pytest.fixture()
def orders_app(monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, StubOrdersClient]:
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
    return app, client


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
    orders_app: tuple[FastAPI, StubOrdersClient]
) -> None:
    app, client_stub = orders_app

    with TestClient(app) as client:
        response = client.post("/api/place_order", json=_order_payload())

    assert response.status_code == status.HTTP_200_OK
    payload = response.json()
    assert payload["success"] is True
    assert payload["command_id"] == "cmd-123"
    assert payload["status"] == "accepted"
    assert client_stub.calls and client_stub.calls[0][0] == "place_order"


def test_place_order_service_unavailable_returns_503(
    orders_app: tuple[FastAPI, StubOrdersClient]
) -> None:
    app, client_stub = orders_app
    client_stub.place_order_response = orders_module.ServiceUnavailableError(
        "orders", "offline"
    )

    with TestClient(app) as client:
        response = client.post("/api/place_order", json=_order_payload())

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert response.json()["detail"] == "Orders service unavailable"


def test_place_order_propagates_service_error_detail(
    orders_app: tuple[FastAPI, StubOrdersClient]
) -> None:
    app, client_stub = orders_app
    client_stub.place_order_response = orders_module.ServiceResponseError(
        "orders", status_code=422, detail="invalid"
    )

    with TestClient(app) as client:
        response = client.post("/api/place_order", json=_order_payload())

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json()["detail"] == "invalid"


def test_cancel_order_uses_client(
    orders_app: tuple[FastAPI, StubOrdersClient]
) -> None:
    app, client_stub = orders_app

    with TestClient(app) as client:
        response = client.post("/api/cancel_order", json={"order_id": "123"})

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["command_id"] == "cmd-cancel"
    assert client_stub.calls and client_stub.calls[-1] == (
        "cancel_order",
        {"order_id": "123"},
    )
