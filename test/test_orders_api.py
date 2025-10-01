from http import HTTPStatus
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.ib_command_service import CommandServiceStatus
from src.utils import now_local


def _orders_module():
    from src.routes import orders as orders_module

    return orders_module


@pytest.fixture
def orders_env(monkeypatch):
    module = _orders_module()

    env = SimpleNamespace(
        module=module,
        redis=object(),
        status=CommandServiceStatus(
            connected=True,
            health={"status": "ok"},
            error=None,
            checked_at=now_local(),
        ),
        events=[],
        enqueue_calls=[],
        wait_calls=[],
        next_command_id=1,
        wait_result={"status": "ok", "data": {"id": 1}},
        wait_timeout=False,
    )

    async def prepare_stub(*, require_connected=True):
        if require_connected and not env.status.connected:
            detail = env.status.error or "IB command service unavailable"
            raise HTTPException(status_code=503, detail=detail)
        return env.redis, env.status

    async def enqueue_stub(redis, command, payload, **kwargs):
        assert redis is env.redis
        env.enqueue_calls.append({
            "command": command,
            "payload": payload,
            "kwargs": kwargs,
        })
        command_id = f"cmd-{env.next_command_id}"
        env.next_command_id += 1
        return command_id

    async def wait_stub(redis, command_id, **kwargs):
        assert redis is env.redis
        env.wait_calls.append({"command_id": command_id, "kwargs": kwargs})
        if env.wait_timeout:
            return None
        return env.wait_result

    async def broadcast_stub(event):
        env.events.append(event)

    monkeypatch.setattr(module, "_prepare_command_service", prepare_stub, raising=False)
    monkeypatch.setattr(module, "enqueue_ib_command", enqueue_stub)
    monkeypatch.setattr(module, "wait_for_command_result", wait_stub)
    monkeypatch.setattr(module.websocket_manager, "broadcast", broadcast_stub, raising=False)

    return env


def test_place_order_requires_command_service(client, admin_headers, orders_env):
    env = orders_env
    env.status.connected = False
    env.status.error = "offline"

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


def test_place_order_enqueues_command(client, admin_headers, orders_env):
    env = orders_env

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
    assert payload["command_id"] == "cmd-1"
    assert payload["status"] == "queued"
    assert env.enqueue_calls[0]["command"] == "place_order"
    submit_payload = env.enqueue_calls[0]["payload"]
    assert submit_payload["action"] == "BUY"
    assert submit_payload["order_type"] == "MARKET"
    assert submit_payload["meta"]["source"] == "manual"
    assert env.events[0]["command_id"] == "cmd-1"
    assert env.events[0]["symbol"] == "TSLA"


def test_cancel_order_wait_parameters(client, admin_headers, orders_env):
    env = orders_env

    response = client.delete(
        "/api/cancel_order/42",
        headers=admin_headers,
        params={"wait": True, "timeout": 7},
    )

    assert response.status_code == HTTPStatus.OK
    data = response.json()
    assert data["command_id"] == "cmd-1"
    assert data["waited"] is True
    assert env.enqueue_calls[0]["command"] == "cancel_order"
    assert env.enqueue_calls[0]["payload"]["order_id"] == "42"
    assert env.wait_calls[0]["command_id"] == "cmd-1"
    assert env.wait_calls[0]["kwargs"]["timeout"] == pytest.approx(7.0)


def test_cancel_order_wait_timeout_returns_error(client, admin_headers, orders_env):
    env = orders_env
    env.wait_timeout = True

    response = client.delete(
        "/api/cancel_order/99",
        headers=admin_headers,
        params={"wait": True},
    )

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["status"] == "timeout"
    assert payload["success"] is False
    assert payload["error"] == "Command result not available"


def test_close_position_enqueues_command(client, admin_headers, orders_env):
    env = orders_env

    response = client.post(
        "/api/close_position",
        headers=admin_headers,
        json={"symbol": "MSFT"},
    )

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["command_id"] == "cmd-1"
    assert env.enqueue_calls[0]["command"] == "close_position"
    assert env.enqueue_calls[0]["payload"]["symbol"] == "MSFT"
    assert env.events[0]["symbol"] == "MSFT"


@pytest.mark.asyncio
async def test_submit_command_contract_matches_broadcast(orders_env):
    env = orders_env
    module = env.module

    env.wait_result = {"status": "ok", "data": {"symbol": "CL"}}

    result = await module._submit_command(
        "close_position",
        {"symbol": "CL"},
        user="admin",
        wait=True,
        timeout=3,
        broadcast_extra={"symbol": "CL"},
    )

    assert result["result"] == env.wait_result
    assert result["command_id"] == env.events[-1]["command_id"]
    assert env.events[-1]["symbol"] == "CL"
    assert result["waited"] is True
