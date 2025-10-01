from datetime import timedelta
from http import HTTPStatus
from types import SimpleNamespace

import pytest


@pytest.fixture
def account_test_env(monkeypatch):
    from src.routes import account as account_module

    now = account_module.now_local()

    state = SimpleNamespace(
        snapshots={
            "account_info": {
                "data": {"account": "DU12345", "equity": 125000.5},
                "updated_at": now.isoformat(),
                "source": "redis",
            },
            "positions": {
                "data": [{"symbol": "ES", "position": 2}],
                "updated_at": now.isoformat(),
                "source": "redis",
            },
        },
        health={"status": "ok", "updated_at": now.isoformat()},
        command_results={
            "get_account_info": ({"snapshot": {"equity": 126000.0}}, None),
            "get_positions": ([{"symbol": "ES", "position": 1}], None),
        },
        http_results={
            "account_info": (None, None),
            "positions": (None, None),
        },
        last_command=None,
    )

    async def _read_account_info_snapshot():
        return state.snapshots.get("account_info")

    async def _read_positions_snapshot():
        return state.snapshots.get("positions")

    async def _read_health():
        return state.health

    async def _read_net_liquidation_snapshot():
        snapshot = state.snapshots.get("account_info")
        if not snapshot:
            return None
        return {
            "value": snapshot["data"].get("equity"),
            "timestamp": snapshot.get("updated_at"),
            "source": snapshot.get("source", "redis"),
        }

    async def _write_net_liquidation_snapshot(*_args, **_kwargs):
        return None

    async def _load_all_daily_totals(*_args, **_kwargs):
        return {}

    async def _noop(*_args, **_kwargs):
        return None

    async def _request_snapshot_refresh(command, payload, *, timeout=None):
        state.last_command = {
            "command": command,
            "payload": payload,
            "timeout": timeout,
        }
        return state.command_results.get(command, (None, "Command not configured"))

    async def _http_refresh_account_info(reason: str):
        return state.http_results.get("account_info", (None, None))

    async def _http_refresh_positions(reason: str, *, fast: bool):
        return state.http_results.get("positions", (None, None))

    monkeypatch.setattr(account_module, "read_account_info_snapshot", _read_account_info_snapshot)
    monkeypatch.setattr(account_module, "read_positions_snapshot", _read_positions_snapshot)
    monkeypatch.setattr(account_module, "read_account_service_health", _read_health)
    monkeypatch.setattr(account_module, "read_net_liquidation_snapshot", _read_net_liquidation_snapshot)
    monkeypatch.setattr(account_module, "write_net_liquidation_snapshot", _write_net_liquidation_snapshot)
    monkeypatch.setattr(account_module, "load_all_daily_totals", _load_all_daily_totals)
    monkeypatch.setattr(account_module, "rebuild_account_daily_cache", _noop)
    monkeypatch.setattr(account_module, "_request_snapshot_refresh", _request_snapshot_refresh)
    monkeypatch.setattr(account_module, "_http_refresh_account_info", _http_refresh_account_info)
    monkeypatch.setattr(account_module, "_http_refresh_positions", _http_refresh_positions)

    class StubDB:
        def fetch_orders_for_pnl(self):
            return []

    monkeypatch.setattr(
        account_module.core_container,
        "get_db_manager",
        lambda: StubDB(),
    )

    return SimpleNamespace(module=account_module, state=state)


def test_backfill_commissions_requires_connection(client, admin_headers):
    response = client.post(
        "/api/orders/backfill_commissions",
        headers=admin_headers,
        params={"days": 3},
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["detail"] == "Not connected to IBKR"


def test_get_account_info_uses_cached_snapshot(client, admin_headers, account_test_env):
    response = client.get("/api/account_info", headers=admin_headers)
    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["success"] is True
    assert payload["from_cache"] is True
    assert payload["account_info"]["equity"] == pytest.approx(125000.5)
    assert payload["cache_source"] == "redis"
    assert payload["stale"] is False


def test_get_account_info_refresh_timeout_returns_error(client, admin_headers, account_test_env):
    env = account_test_env.state
    env.snapshots["account_info"] = None
    env.health = {"status": "offline", "updated_at": env.snapshots.get("positions", {}).get("updated_at")}
    env.command_results["get_account_info"] = (None, "Command timeout")
    env.http_results["account_info"] = (None, None)

    response = client.get("/api/account_info", headers=admin_headers, params={"force_refresh": "true"})
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    payload = response.json()
    assert payload["success"] is False
    assert payload["error"] == "Command timeout"
    assert payload["connected"] is False


def test_get_positions_returns_stale_snapshot(client, admin_headers, account_test_env):
    env = account_test_env.state
    stale_ts = account_test_env.module.now_local() - timedelta(minutes=10)
    env.snapshots["positions"] = {
        "data": [{"symbol": "MNQ", "position": -1}],
        "updated_at": stale_ts.isoformat(),
        "source": "redis",
        "stale": True,
        "stale_reason": "redis_expired",
    }

    response = client.get("/api/positions", headers=admin_headers)
    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["positions"] == env.snapshots["positions"]["data"]
    assert payload["stale"] is True
    assert payload["stale_reason"] == "redis_expired"
    assert payload["from_cache"] is True


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
        return {"status": "ok", "data": {"snapshot": {"positions": []}}}

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
