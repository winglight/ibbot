from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from services.account.main import (
    DEFAULT_ORDER_LIMIT,
    RECENT_ORDERS_CACHE_KEY,
    app,
    _state,
)


@pytest.fixture(autouse=True)
def reset_state() -> None:
    _state.last_refresh_request = None
    _state.redis = None
    _state.health_interval = 0  # disable background loop for deterministic tests
    _state.health_task = None
    _state.watchdog_task = None
    _state.watchdog_stop_event = None
    _state.snapshot_service = None
    _state.stale = False
    _state.last_refresh_error = None
    _state.snapshot_updated_at = None


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
    class _StubSnapshotService:
        def __init__(self) -> None:
            self.refresh_interval_seconds = 0
            self.refresh_account_info_calls: List[Dict[str, Any]] = []
            self.refresh_positions_calls: List[Dict[str, Any]] = []
            self.refresh_both_calls: List[Dict[str, Any]] = []
            self.on_refresh_account_info = None
            self.on_refresh_positions = None
            self.on_refresh_both = None

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

        def request_immediate_refresh(self, reason: str = "connection_restored") -> None:
            return None

        async def refresh_account_info(self, *, reason: str = "manual") -> None:
            self.refresh_account_info_calls.append({"reason": reason})
            if self.on_refresh_account_info:
                await self.on_refresh_account_info(reason=reason)

        async def refresh_positions(self, *, reason: str = "manual", fast: bool = True) -> None:
            self.refresh_positions_calls.append({"reason": reason, "fast": fast})
            if self.on_refresh_positions:
                await self.on_refresh_positions(reason=reason, fast=fast)

        async def refresh_both(self, *, reason: str = "manual", fast_positions: bool = True) -> None:
            self.refresh_both_calls.append({"reason": reason, "fast_positions": fast_positions})
            if self.on_refresh_both:
                await self.on_refresh_both(reason=reason, fast_positions=fast_positions)

    monkeypatch.setattr(
        "services.account.main.AccountSnapshotService", _StubSnapshotService
    )

    async def _fake_request_ib_command(*_: Any, **__: Any) -> Dict[str, Any]:
        return {"status": "success"}

    monkeypatch.setattr("services.account.main.request_ib_command", _fake_request_ib_command)

    with TestClient(app) as client:
        yield client


def _write_snapshot(client: Any, key: str, payload: Dict[str, Any]) -> None:
    async def _set() -> None:
        await client.set(key, json.dumps(payload))

    asyncio.run(_set())


def _write_orders_cache(client: Any, payload: Dict[str, Any]) -> None:
    async def _set() -> None:
        await client.set(RECENT_ORDERS_CACHE_KEY, json.dumps(payload))

    asyncio.run(_set())


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
    assert data["snapshot_updated_at"] == payload["updated_at"]
    assert data["source"] == payload["source"]
    assert data["stale"] is payload["stale"]
    assert data["stale_reason"] is payload.get("stale_reason")
    assert data["stale_at"] is payload.get("stale_at")


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
    assert body["source"] == payload["source"]
    assert body["snapshot_updated_at"] == payload["updated_at"]


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
    orders_payload = orders_response.json()
    assert orders_payload["success"] is True
    assert orders_payload["orders"] == []
    assert orders_payload["from_cache"] is True
    assert orders_payload["meta"] == {
        "page": 1,
        "limit": DEFAULT_ORDER_LIMIT,
        "total": 0,
        "returned": 0,
        "has_more": False,
    }
    assert "source" in orders_payload
    assert "snapshot_updated_at" in orders_payload

    summary_response = test_client.get("/account/daily-summary")
    assert summary_response.status_code == 200
    assert summary_response.json() == {
        "success": True,
        "daily_summary": [],
        "from_cache": True,
    }


def test_account_info_force_refresh_triggers_service(
    test_client: TestClient, fake_redis: Any
) -> None:
    initial = {
        "data": {"equity": 100},
        "updated_at": "2024-01-01T00:00:00+00:00",
        "source": "initial",
        "stale": False,
    }
    refreshed = {
        "data": {"equity": 200},
        "updated_at": "2024-01-01T01:00:00+00:00",
        "source": "refreshed",
        "stale": False,
    }
    _write_snapshot(fake_redis, "account:info", initial)

    service = _state.snapshot_service
    assert service is not None

    async def _update_snapshot(**_: Any) -> None:
        await fake_redis.set("account:info", json.dumps(refreshed))

    service.on_refresh_account_info = _update_snapshot

    response = test_client.get("/account/info", params={"force_refresh": "true"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["account_info"] == refreshed["data"]
    assert payload["source"] == "refreshed"
    assert service.refresh_account_info_calls
    assert service.refresh_account_info_calls[-1]["reason"] == "api-force-refresh"


def test_account_info_stale_snapshot_refreshes_automatically(
    test_client: TestClient, fake_redis: Any
) -> None:
    stale_snapshot = {
        "data": {"equity": 100},
        "updated_at": "2023-12-31T23:00:00+00:00",
        "source": "stale",
        "stale": True,
        "stale_reason": "disconnected",
    }
    refreshed = {
        "data": {"equity": 150},
        "updated_at": "2024-01-01T02:00:00+00:00",
        "source": "fresh",
        "stale": False,
    }
    _write_snapshot(fake_redis, "account:info", stale_snapshot)

    service = _state.snapshot_service
    assert service is not None

    async def _refresh(**_: Any) -> None:
        await fake_redis.set("account:info", json.dumps(refreshed))

    service.on_refresh_account_info = _refresh

    response = test_client.get("/account/info")
    assert response.status_code == 200
    payload = response.json()
    assert payload["account_info"] == refreshed["data"]
    assert service.refresh_account_info_calls
    assert service.refresh_account_info_calls[-1]["reason"] == "api-stale"


def test_positions_force_refresh_respects_fast_flag(
    test_client: TestClient, fake_redis: Any
) -> None:
    initial = {
        "data": [{"symbol": "AAPL", "position": 5}],
        "updated_at": "2024-01-01T00:00:00+00:00",
        "source": "initial",
        "stale": False,
    }
    refreshed = {
        "data": [{"symbol": "AAPL", "position": 15}],
        "updated_at": "2024-01-01T01:00:00+00:00",
        "source": "refreshed",
        "stale": False,
    }
    _write_snapshot(fake_redis, "account:positions", initial)

    service = _state.snapshot_service
    assert service is not None

    async def _refresh_positions(**_: Any) -> None:
        await fake_redis.set("account:positions", json.dumps(refreshed))

    service.on_refresh_positions = _refresh_positions

    response = test_client.get(
        "/account/positions",
        params={"force_refresh": "true", "fast": "false"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["positions"] == refreshed["data"]
    assert service.refresh_positions_calls
    last_call = service.refresh_positions_calls[-1]
    assert last_call["reason"] == "api-force-refresh"
    assert last_call["fast"] is False


def test_account_orders_fast_path_returns_metadata(
    test_client: TestClient, fake_redis: Any
) -> None:
    orders_payload = {
        "orders": [
            {"order_id": 1, "symbol": "AAPL"},
            {"order_id": 2, "symbol": "MSFT"},
        ],
        "updated_at": "2024-01-01T05:00:00+00:00",
        "source": "orders-cache",
        "stale": False,
        "total": 2,
    }
    _write_orders_cache(fake_redis, orders_payload)

    response = test_client.get(
        "/account/orders",
        params={"limit": 1, "page": 2, "fast": "true"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["orders"] == [orders_payload["orders"][1]]
    assert payload["meta"] == {
        "page": 2,
        "limit": 1,
        "total": 2,
        "returned": 1,
        "has_more": False,
    }
    assert payload["source"] == "orders-cache"
    assert payload["snapshot_updated_at"] == orders_payload["updated_at"]
    assert payload["from_cache"] is True


def test_account_orders_force_refresh_triggers_snapshot_refresh(
    test_client: TestClient, fake_redis: Any
) -> None:
    service = _state.snapshot_service
    assert service is not None

    async def _noop_refresh(**_: Any) -> None:
        return None

    service.on_refresh_both = _noop_refresh

    response = test_client.get(
        "/account/orders",
        params={"force_refresh": "true", "fast": "false"},
    )
    assert response.status_code == 200
    assert service.refresh_both_calls
    last_call = service.refresh_both_calls[-1]
    assert last_call["reason"] == "api-orders-force-refresh"
    assert last_call["fast_positions"] is False
