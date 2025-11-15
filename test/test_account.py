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
    monkeypatch.setattr(account_module, "write_account_info_snapshot", _noop_async)
    monkeypatch.setattr(account_module, "read_positions_snapshot", _none_async)
    monkeypatch.setattr(account_module, "write_positions_snapshot", _noop_async)
    monkeypatch.setattr(account_module, "load_all_daily_totals", _empty_dict_async)
    monkeypatch.setattr(account_module, "read_net_liquidation_snapshot", _none_async)
    monkeypatch.setattr(account_module, "write_net_liquidation_snapshot", _noop_async)

    class StubDB:
        def fetch_orders_for_pnl(self):
            return []

    monkeypatch.setattr(account_module, "get_db_manager", lambda: StubDB())

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
    assert response.status_code == HTTPStatus.BAD_REQUEST
    payload = response.json()
    assert payload["success"] is False
    assert payload["connected"] is False
    assert payload["error"] == "Not connected to IBKR"


def test_get_positions_returns_placeholder(client, admin_headers, account_test_env):
    response = client.get("/api/positions", headers=admin_headers)
    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["success"] is True
    assert payload["positions"] == []
    assert payload["warning"] == "Not connected to IBKR"


def test_daily_summary_returns_default_window(client, admin_headers, account_test_env):
    response = client.get("/api/account/daily-summary", headers=admin_headers)
    assert response.status_code == HTTPStatus.OK
    data = response.json()
    assert data["success"] is True
    assert data["days"]
    assert data["range"]["start"] <= data["range"]["end"]
    assert data["surplus_source"] in {"unknown", "risk_manager", "ibkr", "ibkr_cached", "redis"}
