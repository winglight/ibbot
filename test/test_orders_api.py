from http import HTTPStatus

from src.ib_command_service import CommandServiceStatus
from src.utils import now_local


def _orders_module():
    from src.routes import orders as orders_module

    return orders_module


def test_place_order_requires_command_service(client, admin_headers, monkeypatch):
    module = _orders_module()

    status = CommandServiceStatus(
        connected=False,
        health=None,
        error="offline",
        checked_at=now_local(),
    )

    async def status_stub(*_args, **_kwargs):
        return status

    async def redis_stub(*_args, **_kwargs):  # pragma: no cover - defensive
        return object()

    monkeypatch.setattr(module, "get_ib_command_service_status", status_stub)
    monkeypatch.setattr(module, "ensure_ib_command_redis", redis_stub)

    response = client.post(
        "/api/place_order",
        headers=admin_headers,
        json={
            "symbol": "AAPL",
            "quantity": 1,
            "action": "BUY",
            "order_type": "MARKET",
        },
    )

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json()["detail"] == "offline"


def test_place_order_enqueues_command(client, admin_headers, monkeypatch):
    module = _orders_module()
    captured: dict = {}

    async def submit_stub(command, payload, *, user, wait, timeout, broadcast_extra):
        captured["command"] = command
        captured["payload"] = payload
        captured["user"] = user
        captured["wait"] = wait
        captured["timeout"] = timeout
        captured["broadcast"] = broadcast_extra
        return {
            "success": True,
            "command_id": "cmd-789",
            "status": "queued",
            "waited": False,
            "timeout": 20.0,
        }

    monkeypatch.setattr(module, "_submit_command", submit_stub)

    response = client.post(
        "/api/place_order",
        headers=admin_headers,
        json={
            "symbol": "TSLA",
            "quantity": 5,
            "action": "buy",
            "order_type": "market",
            "exchange": "SMART",
        },
    )

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["command_id"] == "cmd-789"
    assert payload["status"] == "queued"
    assert captured["command"] == "place_order"
    assert captured["payload"]["action"] == "BUY"
    assert captured["payload"]["order_type"] == "MARKET"
    assert captured["payload"]["meta"]["source"] == "manual"
    assert captured["wait"] is False
    assert captured["broadcast"]["symbol"] == "TSLA"


def test_cancel_order_wait_parameters(client, admin_headers, monkeypatch):
    module = _orders_module()
    captured: dict = {}

    async def submit_stub(command, payload, *, user, wait, timeout, broadcast_extra):
        captured["command"] = command
        captured["payload"] = payload
        captured["wait"] = wait
        captured["timeout"] = timeout
        return {"success": True, "command_id": "cmd-555", "status": "queued", "waited": wait, "timeout": timeout or 20.0}

    monkeypatch.setattr(module, "_submit_command", submit_stub)

    response = client.delete(
        "/api/cancel_order/42",
        headers=admin_headers,
        params={"wait": True, "timeout": 7},
    )

    assert response.status_code == HTTPStatus.OK
    data = response.json()
    assert data["command_id"] == "cmd-555"
    assert captured["command"] == "cancel_order"
    assert captured["payload"]["order_id"] == "42"
    assert captured["wait"] is True
    assert captured["timeout"] == 7


def test_close_position_enqueues_command(client, admin_headers, monkeypatch):
    module = _orders_module()
    captured: dict = {}

    async def submit_stub(command, payload, *, user, wait, timeout, broadcast_extra):
        captured["command"] = command
        captured["payload"] = payload
        captured["broadcast"] = broadcast_extra
        return {"success": True, "command_id": "cmd-close", "status": "queued", "waited": False, "timeout": 20.0}

    monkeypatch.setattr(module, "_submit_command", submit_stub)

    response = client.post(
        "/api/close_position",
        headers=admin_headers,
        json={"symbol": "MSFT"},
    )

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["command_id"] == "cmd-close"
    assert captured["command"] == "close_position"
    assert captured["payload"]["symbol"] == "MSFT"
    assert captured["broadcast"]["symbol"] == "MSFT"
