from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from services.account.main import app, _state
from src.utils import NY_TZ, now_local, to_local_iso


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

    import src.utils.account_pnl_cache as pnl_cache

    monkeypatch.setattr(pnl_cache, "ensure_redis", _fake_ensure_client)

    return client


@pytest.fixture
def test_client(fake_redis: Any, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    class _StubSnapshotService:
        def __init__(self) -> None:
            self.refresh_interval_seconds = 0
            self.info_refreshes: List[str] = []
            self.positions_refreshes: List[tuple[str, bool]] = []

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

        def request_immediate_refresh(self, reason: str = "connection_restored") -> None:
            return None

        async def refresh_account_info(self, reason: str = "manual") -> Dict[str, Any]:
            self.info_refreshes.append(reason)
            payload = {
                "data": {"equity": 9999},
                "updated_at": to_local_iso(now_local()),
                "source": "stub",
            }
            await fake_redis.set("account:info", json.dumps(payload))
            return payload["data"]

        async def refresh_positions(
            self, *, reason: str = "manual", fast: bool = True
        ) -> List[Dict[str, Any]]:
            self.positions_refreshes.append((reason, fast))
            payload = {
                "data": [{"symbol": "REFRESH", "position": 3}],
                "updated_at": to_local_iso(now_local()),
                "source": "stub",
            }
            await fake_redis.set("account:positions", json.dumps(payload))
            return payload["data"]

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


def test_get_account_info_success(test_client: TestClient, fake_redis: Any) -> None:
    payload = {
        "data": {"equity": 12345},
        "updated_at": to_local_iso(now_local()),
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
    assert data.get("refresh_attempted") is False
    assert data.get("refresh_succeeded") is False
    assert data.get("refresh_requested") is False


def test_get_account_info_missing_snapshot_refreshes(test_client: TestClient) -> None:
    response = test_client.get("/account/info")
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["account_info"]["equity"] == 9999
    assert data["refresh_attempted"] is True
    assert data["from_cache"] is False


def test_get_positions_success(test_client: TestClient, fake_redis: Any) -> None:
    payload = {
        "data": [{"symbol": "AAPL", "position": 10}],
        "updated_at": to_local_iso(now_local()),
        "source": "test",
        "stale": False,
    }
    _write_snapshot(fake_redis, "account:positions", payload)

    response = test_client.get("/account/positions")
    assert response.status_code == 200
    body = response.json()
    assert body["positions"] == payload["data"]
    assert body["from_cache"] is True
    assert body.get("fast") is True
    assert body.get("refresh_attempted") is False


def test_get_positions_missing_snapshot_refreshes(test_client: TestClient) -> None:
    response = test_client.get("/account/positions")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["positions"] == [{"symbol": "REFRESH", "position": 3}]
    assert body["refresh_attempted"] is True
    assert body["from_cache"] is False


def test_stale_snapshot_triggers_refresh(
    test_client: TestClient, fake_redis: Any
) -> None:
    payload = {
        "data": {"equity": 100},
        "updated_at": to_local_iso(now_local()),
        "source": "stale-test",
        "stale": True,
    }
    _write_snapshot(fake_redis, "account:info", payload)

    response = test_client.get("/account/info")
    assert response.status_code == 200
    data = response.json()
    assert data["refresh_attempted"] is True
    assert data["refresh_succeeded"] is True
    assert data["from_cache"] is False
    assert data["account_info"]["equity"] == 9999
    assert data["cache_source"] == "stub"

    service = _state.snapshot_service
    assert service is not None
    assert getattr(service, "info_refreshes", [])
    assert service.info_refreshes[-1] == "api_auto_refresh"


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


def test_orders_and_daily_summary_endpoints(
    test_client: TestClient, fake_redis: Any
) -> None:
    async def _prepare_orders() -> None:
        orders_payload = {
            "active_orders": [{"id": 1, "symbol": "AAPL"}],
            "history": [{"id": 2, "symbol": "MSFT"}],
            "updated_at": to_local_iso(now_local()),
            "source": "orders",
        }
        await fake_redis.set("json:orders:snapshot", json.dumps(orders_payload))

    async def _prepare_daily_summary() -> None:
        today = datetime.now(NY_TZ).date().isoformat()
        await fake_redis.hset("account:daily_pnl", {today: "150.25"})
        await fake_redis.hset("account:daily_orders", {today: "3"})
        net_payload = {
            "value": 5000.0,
            "timestamp": to_local_iso(now_local()),
            "source": "redis-cache",
        }
        await fake_redis.set("account:net_liquidation", json.dumps(net_payload))

    asyncio.run(_prepare_orders())
    asyncio.run(_prepare_daily_summary())

    orders_response = test_client.get("/account/orders", params={"limit": 10})
    assert orders_response.status_code == 200
    orders_data = orders_response.json()
    assert orders_data["success"] is True
    assert orders_data["from_cache"] is True
    assert len(orders_data["orders"]) == 2
    assert len(orders_data["active_orders"]) == 1
    assert len(orders_data["history"]) == 1
    assert orders_data["meta"]["total"] == 2

    summary_response = test_client.get("/account/daily-summary", params={"days": 1})
    assert summary_response.status_code == 200
    summary_data = summary_response.json()
    assert summary_data["success"] is True
    assert summary_data["from_cache"] is True
    assert summary_data["surplus_source"] == "redis-cache"
    assert summary_data["net_liquidation"] == 5000.0
    assert len(summary_data["days"]) == 1
    day_entry = summary_data["days"][0]
    assert day_entry["realized_pnl"] == 150.25
    assert day_entry["orders"] == 3
    assert day_entry["ending_surplus"] == 5000.0
