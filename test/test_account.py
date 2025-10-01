from http import HTTPStatus
from types import SimpleNamespace

import pytest


@pytest.fixture
def account_test_env(monkeypatch):
    from src.routes import account as account_module

    async def _none_async(*_args, **_kwargs):
        return None

    async def _empty_dict_async(*_args, **_kwargs):
        return {}

    async def _noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(account_module, "read_account_info_snapshot", _none_async)
    monkeypatch.setattr(account_module, "read_positions_snapshot", _none_async)
    monkeypatch.setattr(account_module, "load_all_daily_totals", _empty_dict_async)
    monkeypatch.setattr(account_module, "read_net_liquidation_snapshot", _none_async)
    monkeypatch.setattr(account_module, "write_net_liquidation_snapshot", _noop_async)
    monkeypatch.setattr(account_module, "read_account_service_health", _none_async)

    async def _refresh_stub(*_args, **_kwargs):
        return None, "unavailable"

    async def _http_stub(*_args, **_kwargs):
        return None, None

    monkeypatch.setattr(account_module, "_request_snapshot_refresh", _refresh_stub)
    monkeypatch.setattr(account_module, "_http_refresh_account_info", _http_stub)
    monkeypatch.setattr(account_module, "_http_refresh_positions", _http_stub)

    class StubDB:
        def fetch_orders_for_pnl(self):
            return []

    monkeypatch.setattr(
        account_module.core_container,
        "get_db_manager",
        lambda: StubDB(),
    )

    return SimpleNamespace(module=account_module)


def test_backfill_commissions_requires_connection(client, admin_headers):
    response = client.post(
        "/api/orders/backfill_commissions",
        headers=admin_headers,
        params={"days": 3},
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["detail"] == "Not connected to IBKR"


def test_get_account_info_disconnected_uses_error(client, admin_headers, account_test_env):
    response = client.get("/api/account_info", headers=admin_headers)
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    payload = response.json()
    assert payload["success"] is False
    assert payload["connected"] is False
    assert payload["error"] == "unavailable"


def test_get_positions_returns_placeholder(client, admin_headers, account_test_env):
    response = client.get("/api/positions", headers=admin_headers)
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    payload = response.json()
    assert payload["success"] is False
    assert payload["positions"] == []
    assert payload["error"] == "unavailable"


def test_daily_summary_returns_default_window(client, admin_headers, account_test_env):
    response = client.get("/api/account/daily-summary", headers=admin_headers)
    assert response.status_code == HTTPStatus.OK
    data = response.json()
    assert data["success"] is True
    assert data["days"]
    assert data["range"]["start"] <= data["range"]["end"]
    assert data["surplus_source"] in {"unknown", "risk_manager", "account_snapshot", "redis"}


def test_refresh_endpoint_waits_for_result(client, admin_headers, account_test_env, monkeypatch):
    module = account_test_env.module
    dummy = object()
    recorded: dict = {}

    async def ensure_stub(**_kwargs):
        return dummy

    async def enqueue_stub(redis, command, payload, **_kwargs):
        assert redis is dummy
        recorded["command"] = command
        recorded["payload"] = payload
        return "cmd-123"

    async def wait_stub(redis, command_id, **_kwargs):
        assert redis is dummy
        assert command_id == "cmd-123"
        return {"status": "ok", "data": {"snapshot": {}}}

    monkeypatch.setattr(module, "ensure_redis", ensure_stub)
    monkeypatch.setattr(module, "enqueue_ib_command", enqueue_stub)
    monkeypatch.setattr(module, "wait_for_command_result", wait_stub)

    response = client.post(
        "/api/account/refresh",
        headers=admin_headers,
        json={"target": "positions", "fast": False, "reason": "test"},
    )
    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["success"] is True
    assert payload["status"] == "ok"
    assert payload["command_id"] == "cmd-123"
    assert payload["target"] == "positions"
    assert payload["reason"] == "test"
    assert payload["waited"] is True
    assert payload["result"]["status"] == "ok"
    assert recorded["command"] == "get_positions"
    assert recorded["payload"]["fast"] is False


def test_refresh_endpoint_without_wait(client, admin_headers, account_test_env, monkeypatch):
    module = account_test_env.module
    dummy = object()

    async def ensure_stub(**_kwargs):
        return dummy

    async def enqueue_stub(redis, command, payload, **_kwargs):
        assert redis is dummy
        return "cmd-999"

    async def wait_stub(*_args, **_kwargs):
        pytest.fail("wait_for_command_result should not be called when wait=False")

    monkeypatch.setattr(module, "ensure_redis", ensure_stub)
    monkeypatch.setattr(module, "enqueue_ib_command", enqueue_stub)
    monkeypatch.setattr(module, "wait_for_command_result", wait_stub)

    response = client.post(
        "/api/account/refresh",
        headers=admin_headers,
        json={"target": "account_info", "wait": False},
    )
    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["success"] is True
    assert payload["status"] == "queued"
    assert payload["command_id"] == "cmd-999"
    assert payload["waited"] is False
