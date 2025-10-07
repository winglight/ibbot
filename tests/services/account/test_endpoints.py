from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from src.routes.service_clients import ServiceClientError, ServiceUnavailableError

from services.account.main import (
    ACCOUNT_SERVICE_NAME,
    DEFAULT_DAILY_SUMMARY_COMMAND,
    DEFAULT_ORDER_LIMIT,
    app,
    _state,
)


class OrdersClientStub:
    def __init__(
        self,
        *,
        orders: Optional[list[Dict[str, Any]]] = None,
        total: Optional[int] = None,
        updated_at: Optional[str] = None,
        source: str = "orders-service",
        exception: Exception | None = None,
    ) -> None:
        self.orders = list(orders or [])
        self.total = total if total is not None else len(self.orders)
        self.updated_at = updated_at
        self.source = source
        self.exception = exception
        self.calls: List[Dict[str, Any]] = []

    async def list_orders(
        self, *, limit: Optional[int] = None, offset: Optional[int] = None, start_date: Optional[str] = None
    ) -> Dict[str, Any]:
        self.calls.append({"limit": limit, "offset": offset, "start_date": start_date})
        if self.exception is not None:
            raise self.exception
        offset_value = int(offset or 0)
        if limit is None or limit < 0:
            slice_orders = list(self.orders[offset_value:])
        else:
            slice_orders = list(self.orders[offset_value : offset_value + limit])
        return {
            "orders": slice_orders,
            "total": self.total,
            "updated_at": self.updated_at,
            "source": self.source,
            "stale": False,
        }


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
    _state.orders_last_refreshed_at = None
    _state.orders_source = None
    _state.orders_from_cache = None
    _state.orders_stale = None
    _state.orders_stale_reason = None
    _state.orders_last_error = None
    _state.orders_response_time_ms = None
    _state.daily_summary_last_refreshed_at = None
    _state.daily_summary_source = None
    _state.daily_summary_from_cache = None
    _state.daily_summary_stale = None
    _state.daily_summary_stale_reason = None
    _state.daily_summary_last_error = None


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
    import src.utils.account_pnl_cache as pnl_cache

    monkeypatch.setattr(snapshot_cache, "_ensure_client", _fake_ensure_client)
    snapshot_cache._redis_client = client  # type: ignore[attr-defined]
    monkeypatch.setattr(pnl_cache, "ensure_redis", _fake_ensure_client)

    return client


@pytest.fixture
def capture_service_health(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []

    async def _capture(
        redis: Any,
        service_name: str,
        *,
        healthy: Optional[bool] = None,
        status: Optional[str] = None,
        last_error: Optional[str] = None,
        metrics: Optional[Dict[str, Any]] = None,
        key: Optional[str] = None,
        logger: Any | None = None,
    ) -> None:
        calls.append(
            {
                "service": service_name,
                "healthy": healthy,
                "status": status,
                "last_error": last_error,
                "metrics": dict(metrics or {}),
                "key": key,
            }
        )

    monkeypatch.setattr("services.account.main.update_service_health", _capture)
    return calls


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

def _write_daily_summary_cache(
    client: Any,
    totals: Dict[str, Dict[str, Any]],
    net_liq: Optional[Dict[str, Any]] = None,
) -> None:
    async def _write() -> None:
        if totals:
            pnl_mapping = {day: str(values.get("pnl", 0.0)) for day, values in totals.items()}
            orders_mapping = {
                day: str(int(values.get("orders", 0))) for day, values in totals.items()
            }
            await client.hset("account:daily_pnl", pnl_mapping)
            await client.hset("account:daily_orders", orders_mapping)
        if net_liq is not None:
            await client.set("account:net_liquidation", json.dumps(net_liq))

    asyncio.run(_write())


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


def test_account_orders_service_success_updates_metrics(
    capture_service_health: List[Dict[str, Any]],
    test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response_payload = {
        "orders": [
            {"id": "1", "symbol": "AAPL", "quantity": 10},
            {"id": "2", "symbol": "MSFT", "quantity": 5},
            {"id": "3", "symbol": "GOOG", "quantity": 1},
        ],
        "total": 3,
        "updated_at": "2024-01-01T05:00:00+00:00",
        "source": "orders-service",
        "stale": False,
    }
    stub = OrdersClientStub(
        orders=response_payload["orders"],
        total=response_payload["total"],
        updated_at=response_payload["updated_at"],
        source=response_payload["source"],
    )
    monkeypatch.setattr("services.account.main._ORDERS_CLIENT", stub)

    response = test_client.get("/account/orders")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert len(payload["orders"]) == 3
    assert payload["orders"][0]["symbol"] == "AAPL"
    assert "response_time_ms" in payload
    assert isinstance(payload["response_time_ms"], (int, float))
    assert payload["response_time_ms"] >= 0.0
    assert payload["meta"] == {
        "page": 1,
        "limit": DEFAULT_ORDER_LIMIT,
        "total": 3,
        "returned": 3,
        "has_more": False,
    }
    assert payload["source"] == "orders-service"
    assert payload["snapshot_updated_at"] == response_payload["updated_at"]
    assert payload["from_cache"] is False
    assert payload.get("stale") is False

    assert capture_service_health
    metrics = capture_service_health[-1]["metrics"]
    assert capture_service_health[-1]["service"] == ACCOUNT_SERVICE_NAME
    assert metrics.get("orders_last_refreshed_at") == response_payload["updated_at"]
    assert metrics.get("orders_source") == "orders-service"
    assert metrics.get("orders_from_cache") is False
    response_time_metric = metrics.get("orders_response_time_ms")
    assert isinstance(response_time_metric, (int, float))
    assert response_time_metric >= 0.0


def test_account_orders_service_unavailable_returns_503(
    capture_service_health: List[Dict[str, Any]],
    test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = OrdersClientStub(exception=ServiceUnavailableError("orders failed"))
    monkeypatch.setattr("services.account.main._ORDERS_CLIENT", stub)

    response = test_client.get("/account/orders")
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "orders service" in detail.lower() or "orders" in detail.lower()
    assert "orders failed" in detail.lower()

    assert capture_service_health
    metrics = capture_service_health[-1]["metrics"]
    assert capture_service_health[-1]["service"] == ACCOUNT_SERVICE_NAME
    assert metrics.get("orders_last_refreshed_at") == ""
    assert metrics.get("orders_last_error")
    assert "orders failed" in str(metrics.get("orders_last_error", "")).lower()
    assert metrics.get("orders_stale") is True
    assert metrics.get("orders_response_time_ms") == ""


def test_account_daily_summary_cache_success_updates_metrics(
    capture_service_health: List[Dict[str, Any]],
    test_client: TestClient,
    fake_redis: Any,
) -> None:
    daily_totals = {"2024-01-01": {"pnl": 125.5, "orders": 3}}
    net_liq = {
        "value": 150000.0,
        "timestamp": "2024-01-01T15:30:00+00:00",
        "source": "account-service",
    }
    _write_daily_summary_cache(fake_redis, daily_totals, net_liq)

    response = test_client.get("/account/daily-summary")
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["daily_summary"] == [
        {"date": "2024-01-01", "pnl": 125.5, "orders": 3}
    ]
    assert payload["net_liquidation"] == net_liq
    assert payload["from_cache"] is True
    assert payload["source"] == net_liq["source"]
    assert payload["snapshot_updated_at"] == net_liq["timestamp"]
    assert payload.get("stale") in {False, None}

    assert capture_service_health
    metrics = capture_service_health[-1]["metrics"]
    assert capture_service_health[-1]["service"] == ACCOUNT_SERVICE_NAME
    assert metrics.get("daily_summary_last_refreshed_at") == net_liq["timestamp"]
    assert metrics.get("daily_summary_source") == "account-service"


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
    capture_service_health: List[Dict[str, Any]],
    test_client: TestClient,
    fake_redis: Any,
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
    assert payload["source"] == "fresh"
    assert payload["snapshot_updated_at"] == refreshed["updated_at"]
    assert payload["from_cache"] is True
    assert payload["stale"] is False
    assert service.refresh_account_info_calls
    assert service.refresh_account_info_calls[-1]["reason"] == "api-stale"

    assert capture_service_health
    metrics = capture_service_health[-1]["metrics"]
    assert metrics.get("account_snapshot_updated_at") == refreshed["updated_at"]
    assert metrics.get("snapshot_updated_at") == refreshed["updated_at"]


def test_account_info_stale_snapshot_force_refresh_updates_metadata_and_metrics(
    capture_service_health: List[Dict[str, Any]],
    test_client: TestClient,
    fake_redis: Any,
) -> None:
    stale_snapshot = {
        "data": {"equity": 42},
        "updated_at": "2023-12-31T22:00:00+00:00",
        "source": "pre-refresh",
        "stale": True,
    }
    refreshed = {
        "data": {"equity": 84},
        "updated_at": "2024-01-01T04:00:00+00:00",
        "source": "post-refresh",
        "stale": False,
    }
    _write_snapshot(fake_redis, "account:info", stale_snapshot)

    service = _state.snapshot_service
    assert service is not None

    async def _refresh(**_: Any) -> None:
        await fake_redis.set("account:info", json.dumps(refreshed))

    service.on_refresh_account_info = _refresh

    response = test_client.get("/account/info", params={"force_refresh": "true"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["account_info"] == refreshed["data"]
    assert payload["source"] == "post-refresh"
    assert payload["snapshot_updated_at"] == refreshed["updated_at"]
    assert payload["stale"] is False
    assert service.refresh_account_info_calls
    assert service.refresh_account_info_calls[-1]["reason"] == "api-force-refresh"

    assert capture_service_health
    metrics = capture_service_health[-1]["metrics"]
    assert metrics.get("account_snapshot_updated_at") == refreshed["updated_at"]
    assert metrics.get("snapshot_updated_at") == refreshed["updated_at"]


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


def test_positions_stale_snapshot_refreshes_metadata_and_metrics(
    capture_service_health: List[Dict[str, Any]],
    test_client: TestClient,
    fake_redis: Any,
) -> None:
    stale_snapshot = {
        "data": [{"symbol": "MSFT", "position": 2}],
        "updated_at": "2023-12-31T23:30:00+00:00",
        "source": "old",
        "stale": True,
        "stale_reason": "positions-stale",
    }
    refreshed = {
        "data": [{"symbol": "MSFT", "position": 4}],
        "updated_at": "2024-01-01T02:30:00+00:00",
        "source": "fresh-positions",
        "stale": False,
    }
    _write_snapshot(fake_redis, "account:positions", stale_snapshot)

    service = _state.snapshot_service
    assert service is not None

    async def _refresh_positions(**_: Any) -> None:
        await fake_redis.set("account:positions", json.dumps(refreshed))

    service.on_refresh_positions = _refresh_positions

    response = test_client.get("/account/positions")
    assert response.status_code == 200
    payload = response.json()
    assert payload["positions"] == refreshed["data"]
    assert payload["source"] == "fresh-positions"
    assert payload["snapshot_updated_at"] == refreshed["updated_at"]
    assert payload["from_cache"] is True
    assert payload["stale"] is False
    assert service.refresh_positions_calls
    assert service.refresh_positions_calls[-1]["reason"] == "api-stale"

    assert capture_service_health
    metrics = capture_service_health[-1]["metrics"]
    assert metrics.get("positions_snapshot_updated_at") == refreshed["updated_at"]
    assert metrics.get("snapshot_updated_at") == refreshed["updated_at"]


def test_positions_stale_snapshot_force_refresh_updates_metadata_and_metrics(
    capture_service_health: List[Dict[str, Any]],
    test_client: TestClient,
    fake_redis: Any,
) -> None:
    stale_snapshot = {
        "data": [{"symbol": "AAPL", "position": 1}],
        "updated_at": "2023-12-31T21:30:00+00:00",
        "source": "pre-refresh",
        "stale": True,
    }
    refreshed = {
        "data": [{"symbol": "AAPL", "position": 3}],
        "updated_at": "2024-01-01T04:30:00+00:00",
        "source": "post-refresh",
        "stale": False,
    }
    _write_snapshot(fake_redis, "account:positions", stale_snapshot)

    service = _state.snapshot_service
    assert service is not None

    async def _refresh_positions(**_: Any) -> None:
        await fake_redis.set("account:positions", json.dumps(refreshed))

    service.on_refresh_positions = _refresh_positions

    response = test_client.get(
        "/account/positions",
        params={"force_refresh": "true", "fast": "true"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["positions"] == refreshed["data"]
    assert payload["source"] == "post-refresh"
    assert payload["snapshot_updated_at"] == refreshed["updated_at"]
    assert payload["stale"] is False
    assert service.refresh_positions_calls
    last_call = service.refresh_positions_calls[-1]
    assert last_call["reason"] == "api-force-refresh"
    assert last_call["fast"] is True

    assert capture_service_health
    metrics = capture_service_health[-1]["metrics"]
    assert metrics.get("positions_snapshot_updated_at") == refreshed["updated_at"]
    assert metrics.get("snapshot_updated_at") == refreshed["updated_at"]


def test_account_orders_pagination(
    test_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    orders = [
        {"id": "1", "symbol": "AAPL"},
        {"id": "2", "symbol": "MSFT"},
        {"id": "3", "symbol": "TSLA"},
    ]
    stub = OrdersClientStub(
        orders=orders,
        updated_at="2024-01-01T06:00:00+00:00",
    )
    monkeypatch.setattr("services.account.main._ORDERS_CLIENT", stub)

    response = test_client.get(
        "/account/orders",
        params={"limit": 1, "page": 2},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["orders"] == [{"id": "2", "symbol": "MSFT"}]
    assert "response_time_ms" in payload
    assert isinstance(payload["response_time_ms"], (int, float))
    assert payload["meta"] == {
        "page": 2,
        "limit": 1,
        "total": 3,
        "returned": 1,
        "has_more": True,
    }
    assert payload["source"] == "orders-service"
    assert payload["snapshot_updated_at"] == "2024-01-01T06:00:00+00:00"
    assert payload["from_cache"] is False
    assert payload.get("stale") is False

    assert stub.calls
    call = stub.calls[-1]
    assert call["limit"] == 1
    assert call["offset"] == 1


def test_account_orders_force_refresh_triggers_snapshot_refresh(
    test_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _state.snapshot_service
    assert service is not None

    async def _noop_refresh(**_: Any) -> None:
        return None

    service.on_refresh_both = _noop_refresh

    stub = OrdersClientStub(orders=[], updated_at="2024-01-02T00:00:00+00:00")
    monkeypatch.setattr("services.account.main._ORDERS_CLIENT", stub)

    response = test_client.get(
        "/account/orders",
        params={"force_refresh": "true", "fast": "false"},
    )
    assert response.status_code == 200
    assert service.refresh_both_calls
    last_call = service.refresh_both_calls[-1]
    assert last_call["reason"] == "api-orders-force-refresh"
    assert last_call["fast_positions"] is False


def test_account_orders_service_error_returns_502(
    test_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = OrdersClientStub(exception=ServiceClientError("bad response"))
    monkeypatch.setattr("services.account.main._ORDERS_CLIENT", stub)

    response = test_client.get("/account/orders")
    assert response.status_code == 502
    assert "orders service" in response.json()["detail"].lower()


def test_daily_summary_missing_triggers_command_and_returns_503(
    capture_service_health: List[Dict[str, Any]],
    test_client: TestClient,
    fake_redis: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: Dict[str, Any] = {}

    async def _fake_request(method: str, **_: Any) -> Dict[str, Any]:
        called["method"] = method
        return {"status": "success"}

    monkeypatch.setattr("services.account.main.request_ib_command", _fake_request)

    response = test_client.get("/account/daily-summary")
    assert response.status_code == 503
    detail = response.json()["detail"].lower()
    assert "unavailable" in detail
    assert called["method"] == DEFAULT_DAILY_SUMMARY_COMMAND

    assert capture_service_health
    metrics = capture_service_health[-1]["metrics"]
    assert capture_service_health[-1]["service"] == ACCOUNT_SERVICE_NAME
    assert metrics.get("daily_summary_last_refreshed_at") == ""
    assert metrics.get("daily_summary_last_error")
    assert "unavailable" in str(metrics.get("daily_summary_last_error", "")).lower()
    assert metrics.get("daily_summary_stale") is True
