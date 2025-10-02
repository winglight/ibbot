from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from services.account.main import app, _state


@pytest.fixture(autouse=True)
def reset_state() -> None:
    _state.last_refresh_request = None
    _state.redis = None
    _state.health_interval = 0  # disable background loop for deterministic tests
    _state.health_task = None


class _InMemoryRedis:
    def __init__(self) -> None:
        self._kv: Dict[str, str] = {}
        self._hash: Dict[str, Dict[str, str]] = {}

    async def set(self, key: str, value: str) -> None:
        self._kv[key] = value

    async def get(self, key: str) -> str | None:
        return self._kv.get(key)

    async def hset(self, key: str, mapping: Dict[str, Any]) -> int:
        entry = self._hash.setdefault(key, {})
        for field, value in mapping.items():
            entry[str(field)] = value if isinstance(value, str) else json.dumps(value)
        return len(mapping)

    async def hgetall(self, key: str) -> Dict[str, str]:
        return dict(self._hash.get(key, {}))

    async def ping(self) -> bool:  # pragma: no cover - mirrors fakeredis behaviour
        return True

    async def close(self) -> None:  # pragma: no cover - compatibility shim
        return None


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> Any:
    try:  # pragma: no cover - prefer fakeredis when available
        import fakeredis.aioredis as fakeredis  # type: ignore[attr-defined]

        client: Any = fakeredis.FakeRedis(decode_responses=True)
    except Exception:  # pragma: no cover - fallback when fakeredis missing
        client = _InMemoryRedis()

    async def _fake_get_client(*_: Any, **__: Any) -> Any:
        return client

    async def _fake_ensure_client(*_: Any, **__: Any) -> Any:
        return client

    monkeypatch.setattr("services.account.main.get_redis_client", _fake_get_client)

    import src.account_snapshot_cache as snapshot_cache

    monkeypatch.setattr(snapshot_cache, "_ensure_client", _fake_ensure_client)
    snapshot_cache._redis_client = client  # type: ignore[attr-defined]

    return client


@pytest.fixture
def test_client(fake_redis: Any, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    async def _fake_request_ib_command(*_: Any, **__: Any) -> Dict[str, Any]:
        return {"status": "success"}

    monkeypatch.setattr("services.account.main.request_ib_command", _fake_request_ib_command)

    with TestClient(app) as client:
        yield client


def _write_snapshot(client: Any, key: str, payload: Dict[str, Any]) -> None:
    asyncio.get_event_loop().run_until_complete(
        client.set(key, json.dumps(payload)),
    )


def test_get_account_info_success(test_client: TestClient, fake_redis: Any) -> None:
    payload = {
        "data": {"equity": 12345},
        "updated_at": "2024-01-01T12:34:56+00:00",
        "source": "test",
        "stale": False,
    }
    _write_snapshot(fake_redis, "account:info", payload)

    response = test_client.get("/account/info")
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["account_info"] == payload["data"]
    assert data["from_cache"] is True
    assert data["cache_updated_at"] == payload["updated_at"]


def test_get_account_info_missing_snapshot_returns_503(test_client: TestClient) -> None:
    response = test_client.get("/account/info")
    assert response.status_code == 503
    assert "snapshot unavailable" in response.json()["detail"].lower()


def test_get_positions_success(test_client: TestClient, fake_redis: Any) -> None:
    payload = {
        "data": [{"symbol": "AAPL", "position": 10}],
        "updated_at": "2024-01-01T13:00:00+00:00",
        "source": "test",
        "stale": False,
    }
    _write_snapshot(fake_redis, "account:positions", payload)

    response = test_client.get("/account/positions")
    assert response.status_code == 200
    body = response.json()
    assert body["positions"] == payload["data"]
    assert body["from_cache"] is True


def test_get_positions_missing_returns_503(test_client: TestClient) -> None:
    response = test_client.get("/account/positions")
    assert response.status_code == 503
    assert "snapshot unavailable" in response.json()["detail"].lower()


def test_refresh_endpoint_sets_timestamp(test_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    triggered: dict[str, Any] = {"called": False}

    async def _fake_request(*_: Any, **__: Any) -> Dict[str, Any]:
        triggered["called"] = True
        return {"status": "success"}

    monkeypatch.setattr("services.account.main.request_ib_command", _fake_request)

    response = test_client.post("/account/refresh")
    assert response.status_code == 200
    payload = response.json()
    assert "requested_at" in payload
    assert payload["from_cache"] is False
    assert triggered["called"] is True


def test_orders_and_daily_summary_endpoints(test_client: TestClient) -> None:
    orders_response = test_client.get("/account/orders")
    assert orders_response.status_code == 200
    assert orders_response.json() == {"success": True, "orders": [], "from_cache": True}

    summary_response = test_client.get("/account/daily-summary")
    assert summary_response.status_code == 200
    assert summary_response.json() == {
        "success": True,
        "daily_summary": [],
        "from_cache": True,
    }
