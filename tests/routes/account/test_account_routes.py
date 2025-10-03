import collections
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.routes import account as account_module
from src.routes.service_clients import AccountServiceResult, ServiceUnavailableError


@pytest.fixture()
def account_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    app = FastAPI()
    app.include_router(account_module.router)

    async def allow_user(request: Request) -> str:
        request.state.is_read_only = False
        request.state.user_role = "admin"
        request.state.username = "tester"
        return "tester"

    app.dependency_overrides[account_module.get_current_user] = allow_user

    async def noop_snapshot() -> None:
        return None

    monkeypatch.setattr(account_module, "read_account_info_snapshot", noop_snapshot)
    monkeypatch.setattr(account_module, "read_positions_snapshot", noop_snapshot)

    return app


def test_account_info_returns_service_payload(account_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshots = collections.deque([None, None])

    async def fake_read_snapshot() -> Any:
        return snapshots.popleft() if snapshots else None

    monkeypatch.setattr(account_module, "read_account_info_snapshot", fake_read_snapshot)

    class StubClient:
        def __init__(self) -> None:
            self.info_calls: list[bool] = []

        async def fetch_info(
            self, *, force_refresh: bool = False
        ) -> AccountServiceResult:
            self.info_calls.append(force_refresh)
            return AccountServiceResult(
                data={"equity": 123.45, "connected": True},
                metadata={"source": "service", "from_cache": False},
            )

        async def fetch_positions(
            self, *, fast: bool = False, force_refresh: bool = False
        ) -> AccountServiceResult:
            raise AssertionError("positions fetch should not be called")

    stub_client = StubClient()
    monkeypatch.setattr(account_module, "_get_account_service_client", lambda: stub_client)

    with TestClient(account_app) as client:
        response = client.get("/api/account_info")

    assert response.status_code == 200
    payload = response.json()
    assert payload["account_info"]["equity"] == pytest.approx(123.45)
    assert payload["from_cache"] is False
    assert payload["connected"] is True
    assert stub_client.info_calls == [False]


def test_positions_returns_cached_snapshot_when_service_unavailable(account_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = {
        "data": [{"symbol": "ES", "position": 2}],
        "updated_at": "2024-01-01T00:00:00",
        "source": "redis",
    }
    snapshots = collections.deque([snapshot, snapshot])

    async def fake_read_positions_snapshot() -> Any:
        return snapshots.popleft() if snapshots else snapshot

    monkeypatch.setattr(account_module, "read_positions_snapshot", fake_read_positions_snapshot)

    class FailingClient:
        async def fetch_info(self, *, force_refresh: bool = False) -> Any:
            return None

        async def fetch_positions(self, *, fast: bool = False, force_refresh: bool = False) -> Any:
            raise ServiceUnavailableError("account", "offline")

    monkeypatch.setattr(account_module, "_get_account_service_client", lambda: FailingClient())

    with TestClient(account_app) as client:
        response = client.get("/api/positions", params={"force_refresh": "true"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["from_cache"] is True
    assert payload["positions"] == snapshot["data"]
    assert payload.get("warning") == "Account service unavailable"


def test_backfill_commissions_returns_503_when_queue_unavailable(account_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_queue_key() -> str:
        return "queue:test"

    async def failing_enqueue(payload: dict[str, Any], *, queue_key: str) -> None:  # pragma: no cover - executed in test
        raise RuntimeError("redis offline")

    monkeypatch.setattr(account_module, "_resolve_command_queue_key", fake_queue_key)
    monkeypatch.setattr(account_module, "enqueue_ib_command", failing_enqueue)

    with TestClient(account_app) as client:
        response = client.post("/api/orders/backfill_commissions", params={"days": 3})

    assert response.status_code == 503
    assert response.json()["detail"] == "Failed to enqueue backfill commissions command"
