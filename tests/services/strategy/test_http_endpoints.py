"""Tests for the strategy HTTP service endpoints."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from typing import Any, Mapping

import pytest
from fastapi.testclient import TestClient

from services.strategy.discovery import sync_strategy_metadata
from services.strategy.http import (
    DEFAULT_SETTINGS,
    DEFAULT_STRATEGY_QUEUE,
    StrategyRuntime,
    create_app,
    persist_strategy_settings,
)
import src.utils.strategy_cache as strategy_cache_module
from tests.services.redis_utils import InMemoryRedis


@pytest.fixture
def redis_client() -> InMemoryRedis:
    client = InMemoryRedis()
    asyncio.run(client.flushall())
    try:
        yield client
    finally:
        asyncio.run(client.flushall())
        asyncio.run(client.close())


def _build_client(
    redis: InMemoryRedis,
    *,
    runtime: StrategyRuntime | None = None,
    app_kwargs: dict[str, Any] | None = None,
) -> TestClient:
    def redis_factory() -> InMemoryRedis:
        return redis

    kwargs = app_kwargs or {}
    app = create_app(
        redis_factory=redis_factory,
        health_interval=0.05,
        event_poll_interval=0.05,
        runtime=runtime,
        **kwargs,
    )
    return TestClient(app)


def _read_metadata(redis: InMemoryRedis, name: str) -> dict[str, object]:
    raw = asyncio.run(redis.get(f"json:strategy:{name}"))
    if not raw:
        return {}
    if isinstance(raw, str):
        return json.loads(raw)
    return dict(raw)


def _install_strategy_cache_stubs(redis: InMemoryRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ensure() -> InMemoryRedis:
        return redis

    async def _hmget(key: str, fields: list[str]) -> list[str | None]:
        data = await redis.hgetall(key)
        return [data.get(field) for field in fields]

    async def _zrangebyscore(key: str, *_: object, **__: object) -> list[str]:
        return []

    monkeypatch.setattr(strategy_cache_module, "ensure_redis", _ensure)
    setattr(redis, "hmget", _hmget)
    setattr(redis, "zrangebyscore", _zrangebyscore)


def _prepare_strategy_orders(redis: InMemoryRedis, name: str, orders: list[dict[str, object]]) -> None:
    key = f"strategy:{name}:orders"
    mapping = {}
    for order in orders:
        order_id = str(order.get("order_id"))
        mapping[order_id] = json.dumps({**order, "order_id": order_id})
    asyncio.run(redis.hset(key, mapping=mapping))


def _prepare_recent_fills(redis: InMemoryRedis, name: str, fills: list[dict[str, object]]) -> None:
    key = f"strategy:{name}:recent"
    for fill in fills:
        asyncio.run(redis.rpush(key, json.dumps(fill)))


def _read_json_value(redis: InMemoryRedis, key: str) -> Any:
    value = asyncio.run(redis.get(key))
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


class StubRiskManager:
    def __init__(self) -> None:
        self.rule: dict[str, Any] | None = None
        self.loaded: list[str] = []
        self.updated: list[tuple[str, dict[str, Any]]] = []

    async def load_rule(self, symbol: str) -> dict[str, Any] | None:
        self.loaded.append(symbol)
        return self.rule

    async def update_rule(
        self,
        symbol: str,
        payload: Mapping[str, Any] | None = None,
        *,
        enabled: bool | None = None,
    ) -> None:
        update_payload = dict(payload or {})
        if enabled is not None:
            update_payload["enabled"] = enabled
        self.updated.append((symbol, update_payload))
        self.rule = update_payload

def test_discovered_strategy_available_on_start(redis_client: InMemoryRedis) -> None:
    asyncio.run(sync_strategy_metadata(redis_client, {}))
    client = _build_client(redis_client)

    with client:
        response = client.get("/strategy")

    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, list)
    assert any(entry.get("name") == "BuyTheDipStrategy" for entry in payload)


def test_filesystem_strategies_discovered(redis_client: InMemoryRedis, tmp_path: Path) -> None:
    strategy_dir = tmp_path / "strategies"
    strategy_dir.mkdir()
    file_path = strategy_dir / "filesystem_alpha.py"
    file_path.write_text("print('alpha')\n", encoding="utf-8")

    settings = {**DEFAULT_SETTINGS, "strategy_path": str(strategy_dir)}
    asyncio.run(persist_strategy_settings(redis_client, settings))

    client = _build_client(redis_client)

    with client:
        files_response = client.get("/strategy/files")
        assert files_response.status_code == 200
        files_payload = files_response.json()
        assert files_payload["success"] is True
        assert "filesystem_alpha.py" in files_payload["files"]

        strategies_response = client.get("/strategy")
        assert strategies_response.status_code == 200
        strategies = strategies_response.json()
        names = [entry["name"] for entry in strategies]
        assert "filesystem_alpha" in names
        fs_entry = next(entry for entry in strategies if entry["name"] == "filesystem_alpha")
        assert fs_entry["metadata"]["file"] == "filesystem_alpha.py"
        assert fs_entry["metadata"]["source"] == "filesystem"


def test_strategy_crud_lifecycle(redis_client: InMemoryRedis) -> None:
    client = _build_client(redis_client)

    payload = {
        "name": "demo",
        "factory": "tests.services.strategy.sample:factory",
        "params": {"symbol": "ES"},
        "description": "Demo strategy",
        "metadata": {"owner": "qa"},
    }

    with client:
        response = client.get("/strategy")
        assert response.status_code == 200
        initial = response.json()
        assert all(entry["name"] != "demo" for entry in initial)

        response = client.post("/strategies", json=payload)
        assert response.status_code == 201
        created = response.json()
        assert created["name"] == "demo"
        assert created["params"]["symbol"] == "ES"
        assert "created_at" in created
        assert "updated_at" in created

        stored = _read_metadata(redis_client, "demo")
        assert stored["name"] == "demo"
        assert stored["metadata"]["owner"] == "qa"

        response = client.get("/strategy/demo")
        assert response.status_code == 200
        assert response.json()["factory"] == payload["factory"]

        updated_payload = {**payload, "params": {"symbol": "NQ", "size": 2}}
        response = client.put("/strategy", json=updated_payload)
        assert response.status_code == 200
        updated = response.json()
        assert updated["params"]["symbol"] == "NQ"
        assert updated["params"]["size"] == 2
        assert updated["created_at"] == created["created_at"]
        assert updated["updated_at"] != created["updated_at"]

        response = client.delete("/strategy/demo")
        assert response.status_code == 204

    removed = asyncio.run(redis_client.get("json:strategy:demo"))
    assert removed is None


def test_rest_style_strategy_routes(redis_client: InMemoryRedis) -> None:
    client = _build_client(redis_client)

    payload = {
        "name": "rest",
        "factory": "tests.services.strategy.sample:factory",
        "params": {"symbol": "YM"},
        "description": "REST alias",
    }

    with client:
        create_resp = client.post("/strategies", json=payload)
        assert create_resp.status_code == 201
        created = create_resp.json()
        assert created["name"] == payload["name"]

        list_resp = client.get("/strategies")
        assert list_resp.status_code == 200
        names = [entry["name"] for entry in list_resp.json()]
        assert payload["name"] in names

        detail_resp = client.get(f"/strategies/{payload['name']}")
        assert detail_resp.status_code == 200
        assert detail_resp.json()["description"] == payload["description"]

        mismatch_resp = client.put("/strategies/wrong", json=payload)
        assert mismatch_resp.status_code == 400

        update_payload = {**payload, "params": {"symbol": "YM", "size": 3}}
        update_resp = client.put(f"/strategies/{payload['name']}", json=update_payload)
        assert update_resp.status_code == 200
        assert update_resp.json()["params"]["size"] == 3

        delete_resp = client.delete(f"/strategies/{payload['name']}")
        assert delete_resp.status_code == 204

def test_strategy_run_enqueues_tasks(redis_client: InMemoryRedis) -> None:
    client = _build_client(redis_client)

    payload = {
        "name": "momentum",
        "factory": "tests.services.strategy.sample:factory",
        "params": {"window": 5},
    }

    with client:
        assert client.post("/strategies", json=payload).status_code == 201
        response = client.post("/strategy/momentum/run", json={"params": {"symbol": "CL"}})
        assert response.status_code == 202
        queued = response.json()
        assert queued["task_id"].startswith("momentum-")
        assert queued["queue_size"] == 1

        runtime: StrategyRuntime = client.app.state.runtime
        assert runtime.pending_tasks == 1


        asyncio.run(
            redis_client.xadd(
                runtime.event_stream,
                {"status": "completed", "strategy": "momentum", "task_id": queued["task_id"]},
            )
        )

        # Give the background poller time to consume the event.
        asyncio.run(asyncio.sleep(0.1))

        assert runtime.pending_tasks == 0
        assert runtime.last_event_at is not None
        last_event = datetime.fromisoformat(runtime.last_event_at)
        assert last_event <= datetime.now(timezone.utc)


def test_get_strategy_performance_success(
    redis_client: InMemoryRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_strategy_cache_stubs(redis_client, monkeypatch)
    client = _build_client(redis_client)

    payload = {
        "name": "alpha",
        "factory": "tests.services.strategy.sample:factory",
        "params": {"symbol": "ES"},
    }

    orders = [
        {
            "order_id": "order-1",
            "strategy": "alpha",
            "created_at": "2024-01-01T09:50:00+08:00",
            "executed_at": "2024-01-01T10:00:00+08:00",
            "pnl": -20.0,
            "commission": 2.0,
            "duration": 300,
        },
        {
            "order_id": "order-2",
            "strategy": "alpha",
            "created_at": "2024-01-01T11:00:00+08:00",
            "executed_at": "2024-01-01T11:05:00+08:00",
            "pnl": 50.0,
            "commission": 1.0,
            "duration": 600,
        },
    ]

    with client:
        assert client.post("/strategies", json=payload).status_code == 201
        _prepare_strategy_orders(redis_client, "alpha", orders)
        response = client.get(
            "/strategies/alpha/performance",
            params={
                "start": "2023-12-31T16:00:00Z",
                "end": "2024-01-02T00:00:00+08:00",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["name"] == "alpha"
    metrics = payload["metrics"]
    assert metrics["total_executions"] == 2
    assert metrics["total_pnl"] == pytest.approx(30.0)
    assert metrics["avg_pnl"] == pytest.approx(15.0)
    assert metrics["winning_trades"] == 1
    assert metrics["losing_trades"] == 1
    assert metrics["win_rate"] == pytest.approx(50.0)
    assert metrics["max_drawdown"] == pytest.approx(20.0)
    assert metrics["avg_trade_duration"] == pytest.approx(450.0)
    assert metrics["total_commission"] == pytest.approx(3.0)


def test_get_strategy_performance_not_found(
    redis_client: InMemoryRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_strategy_cache_stubs(redis_client, monkeypatch)
    client = _build_client(redis_client)

    with client:
        response = client.get("/strategies/ghost/performance")

    assert response.status_code == 404
    assert response.json()["detail"] == "strategy not found"


def test_get_strategy_performance_invalid_range(
    redis_client: InMemoryRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_strategy_cache_stubs(redis_client, monkeypatch)
    client = _build_client(redis_client)

    payload = {
        "name": "beta",
        "factory": "tests.services.strategy.sample:factory",
        "params": {"symbol": "NQ"},
    }

    with client:
        assert client.post("/strategies", json=payload).status_code == 201
        response = client.get("/strategies/beta/performance", params={"range": "invalid"})

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid range parameter"


def test_strategy_settings_endpoints(redis_client: InMemoryRedis) -> None:
    client = _build_client(redis_client)

    with client:
        response = client.get("/strategy/settings")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        settings = data["settings"]
        assert settings["api_endpoint"] == "/api/strategy"
        assert settings["pause_data_push_on_holidays"] is True

        update_payload = {
            "api_endpoint": "/hook/strategy",
            "api_method": "PUT",
            "api_payload_template": {"hello": "world"},
            "strategy_path": "custom/strategies/",
            "enable_auth": True,
            "pause_data_push_on_holidays": False,
        }
        response = client.post("/strategy/settings", json=update_payload)
        assert response.status_code == 200
        updated = response.json()["settings"]
        assert updated["api_endpoint"] == "/hook/strategy"
        assert updated["pause_data_push_on_holidays"] is False
        assert updated["enable_auth"] is True
        assert updated["api_payload_template"] == {"hello": "world"}

        health = asyncio.run(redis_client.hgetall("hash:service:health:strategy"))
        assert "settings_updated_at" in health

        response = client.post("/strategy/settings/generate-token")
        assert response.status_code == 200
        token_payload = response.json()
        assert token_payload["success"] is True
        assert isinstance(token_payload["token"], str)
        assert token_payload["token"]
        assert token_payload["settings"]["auth_token"] == token_payload["token"]
        assert token_payload["settings"]["enable_auth"] is True

        response = client.get("/strategy/settings")
        persisted = response.json()["settings"]
        assert persisted["auth_token"] == token_payload["token"]


def test_strategy_toggle_aliases(redis_client: InMemoryRedis) -> None:
    client = _build_client(redis_client)

    payload = {
        "name": "switch",
        "factory": "tests.services.strategy.sample:factory",
        "params": {"window": 5},
        "metadata": {"file": "switch.py"},
        "enabled": True,
    }

    with client:
        assert client.post("/strategies", json=payload).status_code == 201

        toggle_resp = client.post("/strategies/switch/toggle?enabled=false")
        assert toggle_resp.status_code == 200
        data = toggle_resp.json()
        assert data["success"] is True
        assert data["strategy"]["enabled"] is False

        stored = _read_metadata(redis_client, "switch")
        assert stored["enabled"] is False

        toggle_resp = client.post("/strategy/switch/toggle", json={"enabled": True})
        assert toggle_resp.status_code == 200
        data = toggle_resp.json()
        assert data["strategy"]["enabled"] is True

        stored = _read_metadata(redis_client, "switch")
        assert stored["enabled"] is True

        bad_resp = client.post("/strategy/switch/toggle")
        assert bad_resp.status_code == 400


def test_strategy_reset_aliases(redis_client: InMemoryRedis) -> None:
    client = _build_client(redis_client)

    payload = {
        "name": "resetme",
        "factory": "tests.services.strategy.sample:factory",
        "params": {"symbol": "ES", "size": 3},
        "metadata": {"file": "resetme.py", "owner": "qa"},
        "enabled": False,
    }

    with client:
        assert client.post("/strategies", json=payload).status_code == 201

        resp = client.post("/strategies/resetme/reset")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["strategy"]["enabled"] is True
        assert data["strategy"]["params"] == {}
        assert data["strategy"]["metadata"] == {}

        stored = _read_metadata(redis_client, "resetme")
        assert stored["enabled"] is True
        assert stored["params"] == {}
        assert stored["metadata"] == {}

        legacy_resp = client.post("/strategy/resetme/reset")
        assert legacy_resp.status_code == 200

        missing_resp = client.post("/strategy/unknown/reset")
        assert missing_resp.status_code == 404


def test_strategy_files_endpoint_merges_sources(
    redis_client: InMemoryRedis, tmp_path: Path
) -> None:
    client = _build_client(redis_client)

    strategy_root = tmp_path / "strategies"
    (strategy_root / "nested").mkdir(parents=True)
    (strategy_root / "alpha.py").write_text("# alpha\n")
    (strategy_root / "nested" / "beta.py").write_text("# beta\n")
    (strategy_root / "notes.txt").write_text("ignore")

    settings = {**DEFAULT_SETTINGS, "strategy_path": str(strategy_root)}
    asyncio.run(persist_strategy_settings(redis_client, settings))

    payload = {
        "name": "manual",
        "factory": "tests.services.strategy.sample:factory",
        "metadata": {"file": "legacy.py"},
    }

    with client:
        assert client.post("/strategies", json=payload).status_code == 201

        response = client.get("/strategy/files")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        files = data["files"]
        assert "alpha.py" in files
        assert "nested/beta.py" in files
        assert "legacy.py" in files

        (strategy_root / "gamma.py").write_text("# gamma\n")
        response = client.get("/strategy/files")
        files = response.json()["files"]
        assert "gamma.py" in files


def test_strategy_executions_endpoint_returns_recent_fills(
    redis_client: InMemoryRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_strategy_cache_stubs(redis_client, monkeypatch)
    client = _build_client(redis_client)

    strategy_name = "exec-demo"
    now = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    orders = [
        {
            "order_id": "200",
            "symbol": "ES",
            "action": "SELL",
            "quantity": 1,
            "price": 4250.0,
            "fill_price": 4250.5,
            "executed_at": (now.replace(minute=55)).isoformat(),
            "created_at": (now.replace(minute=54)).isoformat(),
        },
        {
            "order_id": "100",
            "symbol": "ES",
            "action": "BUY",
            "quantity": 2,
            "price": 4200.0,
            "fill_price": 4201.0,
            "executed_at": (now.replace(minute=50)).isoformat(),
            "created_at": (now.replace(minute=49)).isoformat(),
        },
    ]
    fills = [
        {
            "order_id": "300",
            "symbol": "NQ",
            "action": "BUY",
            "quantity": 1,
            "price": 16000.0,
            "fill_price": 16001.25,
            "executed_at": (now.replace(minute=59)).isoformat(),
            "created_at": (now.replace(minute=58)).isoformat(),
        },
        {
            "order_id": "200",
            "symbol": "ES",
            "action": "SELL",
            "quantity": 1,
            "price": 4250.0,
            "fill_price": 4250.5,
            "executed_at": (now.replace(minute=55)).isoformat(),
            "created_at": (now.replace(minute=54)).isoformat(),
        },
    ]

    _prepare_strategy_orders(redis_client, strategy_name, orders)
    _prepare_recent_fills(redis_client, strategy_name, fills)

    payload = {
        "name": strategy_name,
        "factory": "tests.services.strategy.sample:factory",
        "params": {"symbol": "ES"},
    }

    with client:
        assert client.post("/strategies", json=payload).status_code == 201

        response = client.get(f"/strategies/{strategy_name}/executions", params={"limit": 2})
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert len(data["executions"]) == 2
        ids = [entry.get("order_id") for entry in data["executions"]]
        assert ids == ["300", "200"]
        assert all(entry.get("timestamp") for entry in data["executions"])

        stats_key = f"strategy:{strategy_name}:stats"
        assert asyncio.run(redis_client.hgetall(stats_key)) == {}

        refresh_response = client.get(
            f"/strategies/{strategy_name}/executions",
            params={"limit": 5, "force_refresh": "true"},
        )
        assert refresh_response.status_code == 200
        stats = asyncio.run(redis_client.hgetall(stats_key))
        assert stats.get("stale") == "1"


def test_clear_strategy_executions_endpoint_purges_cache(
    redis_client: InMemoryRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_strategy_cache_stubs(redis_client, monkeypatch)
    client = _build_client(redis_client)

    strategy_name = "cleanup"
    now = datetime(2024, 1, 2, 15, 0, tzinfo=timezone.utc)
    orders = [
        {
            "order_id": "10",
            "symbol": "CL",
            "action": "BUY",
            "quantity": 1,
            "price": 75.5,
            "fill_price": 75.6,
            "executed_at": now.isoformat(),
            "created_at": now.isoformat(),
        }
    ]
    fills = [
        {
            "order_id": "11",
            "symbol": "CL",
            "action": "SELL",
            "quantity": 1,
            "price": 76.0,
            "fill_price": 76.1,
            "executed_at": (now.replace(minute=1)).isoformat(),
            "created_at": (now.replace(minute=1)).isoformat(),
        }
    ]

    _prepare_strategy_orders(redis_client, strategy_name, orders)
    _prepare_recent_fills(redis_client, strategy_name, fills)
    asyncio.run(
        redis_client.hset(
            f"strategy:{strategy_name}:stats",
            mapping={"filled_count": "1", "stale": "0"},
        )
    )
    asyncio.run(
        redis_client.hset(
            f"strategy:{strategy_name}:daily",
            mapping={"2024-01-02": "12.5"},
        )
    )

    payload = {
        "name": strategy_name,
        "factory": "tests.services.strategy.sample:factory",
        "params": {"symbol": "CL"},
    }

    with client:
        assert client.post("/strategies", json=payload).status_code == 201

        response = client.delete(f"/strategies/{strategy_name}/executions")
        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is True
        assert payload["executions"] == []

        orders_hash = asyncio.run(redis_client.hgetall(f"strategy:{strategy_name}:orders"))
        recent_entries = asyncio.run(redis_client.lrange(f"strategy:{strategy_name}:recent", 0, -1))
        stats = asyncio.run(redis_client.hgetall(f"strategy:{strategy_name}:stats"))
        daily = asyncio.run(redis_client.hgetall(f"strategy:{strategy_name}:daily"))

        assert orders_hash == {}
        assert recent_entries == []
        assert stats == {}
        assert daily == {}


def test_submit_external_order_enqueues_message(redis_client: InMemoryRedis) -> None:
    client = _build_client(redis_client)

    payload = {
        "strategy": "alpha",
        "symbol": "ES",
        "action": "BUY",
        "qty": 2,
        "type": "MARKET",
    }

    with client:
        response = client.post("/strategy", json=payload)

    assert response.status_code == 202
    data = response.json()
    assert data["success"] is True
    assert data["order"]["strategy"] == "alpha"

    queued = asyncio.run(redis_client.blpop(DEFAULT_STRATEGY_QUEUE, timeout=0.1))
    assert queued is not None
    _, raw = queued
    message = json.loads(raw)
    assert message["type"] == "external_order"
    assert message["order"]["strategy"] == "alpha"


def test_internal_info_endpoints_round_trip(redis_client: InMemoryRedis) -> None:
    client = _build_client(redis_client)

    payload = {
        "name": "inspector",
        "factory": "tests.services.strategy.sample:factory",
        "description": "Internal strategy",
        "params": {"symbol": "ES"},
        "metadata": {"internal_info": {"parameters": {"window": 5}, "notes": "demo"}},
    }

    with client:
        assert client.post("/strategies", json=payload).status_code == 201

        info_resp = client.get("/strategies/inspector/internal-info")
        assert info_resp.status_code == 200
        info_payload = info_resp.json()
        assert info_payload["success"] is True
        assert info_payload["internal_info"]["parameters"]["window"] == 5

        update_resp = client.post(
            "/strategies/inspector/internal-info/parameter",
            json={"name": "threshold", "value": 42},
        )
        assert update_resp.status_code == 200
        updated = update_resp.json()["internal_info"]
        assert updated["parameters"]["threshold"] == 42

        cached = _read_json_value(redis_client, "json:strategy:inspector:internal")
        assert cached["parameters"]["threshold"] == 42


def test_internal_info_missing_strategy_returns_not_found(redis_client: InMemoryRedis) -> None:
    client = _build_client(redis_client)

    with client:
        resp = client.get("/strategies/ghost/internal-info")
        assert resp.status_code == 404

        update_resp = client.post(
            "/strategies/ghost/internal-info/parameter",
            json={"name": "alpha", "value": 1},
        )
        assert update_resp.status_code == 404


def test_risk_settings_round_trip(redis_client: InMemoryRedis) -> None:
    stub = StubRiskManager()
    stub.rule = {"max_positions": 3, "forbid_add_on_loss": False}
    runtime = StrategyRuntime(redis_client, risk_manager=stub)
    client = _build_client(redis_client, runtime=runtime)

    payload = {
        "name": "riskable",
        "factory": "tests.services.strategy.sample:factory",
        "params": {"symbol": "ES"},
        "metadata": {"primary_symbol": "ES"},
    }

    with client:
        assert client.post("/strategies", json=payload).status_code == 201

        get_resp = client.get("/strategies/riskable/risk-settings")
        assert get_resp.status_code == 200
        settings = get_resp.json()["settings"]
        assert settings["max_positions"] == 3
        assert stub.loaded == ["ES"]

        update_payload = {"max_positions": 5, "loss_pnl_threshold": 125.5}
        put_resp = client.put("/strategies/riskable/risk-settings", json=update_payload)
        assert put_resp.status_code == 200
        persisted = put_resp.json()["settings"]
        assert persisted["max_positions"] == 5
        assert stub.updated[-1][0] == "ES"
        assert stub.rule["loss_pnl_threshold"] == 125.5

        stored = _read_json_value(redis_client, "json:strategy:riskable:risk")
        assert stored["max_positions"] == 5


def test_strategy_status_and_data_push(redis_client: InMemoryRedis) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def handler(name: str, payload: Mapping[str, Any]) -> None:
        calls.append((name, dict(payload)))

    runtime = StrategyRuntime(redis_client, data_push_handler=handler)
    client = _build_client(redis_client, runtime=runtime)

    payload = {
        "name": "status-demo",
        "factory": "tests.services.strategy.sample:factory",
        "params": {"symbol": "NQ"},
    }

    status_key = "json:strategy:status-demo:status"
    asyncio.run(redis_client.set(status_key, json.dumps({"heartbeat": 42})))

    with client:
        assert client.post("/strategies", json=payload).status_code == 201

        status_resp = client.get("/strategies/status-demo/status")
        assert status_resp.status_code == 200
        status_payload = status_resp.json()["status"]
        assert status_payload["heartbeat"] == 42

        request_payload = {
            "enabled": True,
            "subscription": "dom_trend",
            "symbol": "NQ",
        }
        push_resp = client.post("/strategies/status-demo/data-push", json=request_payload)
        assert push_resp.status_code == 200
        stored = _read_json_value(redis_client, "json:strategy:status-demo:data-push")
        assert stored["enabled"] is True
        assert calls and calls[-1][0] == "status-demo"

        refreshed = client.get("/strategies/status-demo/status")
        assert refreshed.json()["status"]["data_push"]["enabled"] is True


def test_strategy_rename_updates_metadata_keys(redis_client: InMemoryRedis) -> None:
    client = _build_client(redis_client)

    payload = {
        "name": "legacy",
        "factory": "tests.services.strategy.sample:factory",
        "params": {"symbol": "CL"},
    }

    asyncio.run(redis_client.set("json:strategy:legacy:internal", json.dumps({"parameters": {"base": 1}})))

    with client:
        assert client.post("/strategies", json=payload).status_code == 201

        rename_resp = client.post("/strategies/legacy/rename", json={"new_name": "renewed"})
        assert rename_resp.status_code == 200
        renamed = rename_resp.json()
        assert renamed["name"] == "renewed"

        missing = client.get("/strategies/legacy")
        assert missing.status_code == 404

        listing = client.get("/strategies")
        names = [entry["name"] for entry in listing.json()]
        assert "renewed" in names

        new_internal = _read_json_value(redis_client, "json:strategy:renewed:internal")
        assert new_internal["parameters"]["base"] == 1
        assert _read_json_value(redis_client, "json:strategy:legacy:internal") is None


def test_daily_pnl_endpoint_uses_cache(redis_client: InMemoryRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    marks: list[str] = []

    async def fake_daily(name: str) -> list[dict[str, Any]]:
        calls.append(name)
        return [{"date": "2024-01-01", "pnl": 10.5}]

    async def fake_mark(name: str) -> None:
        marks.append(name)

    monkeypatch.setattr("services.strategy.http.get_daily_pnl", fake_daily)
    monkeypatch.setattr("services.strategy.http.mark_strategy_cache_stale", fake_mark)

    client = _build_client(redis_client)

    payload = {
        "name": "pnl-demo",
        "factory": "tests.services.strategy.sample:factory",
        "params": {"symbol": "YM"},
    }

    with client:
        assert client.post("/strategies", json=payload).status_code == 201

        resp = client.get("/strategies/pnl-demo/pnl/daily")
        assert resp.status_code == 200
        data = resp.json()
        assert data["pnl"][0]["pnl"] == 10.5
        assert calls == ["pnl-demo"]

        resp_force = client.get(
            "/strategies/pnl-demo/pnl/daily",
            params={"force_refresh": "true"},
        )
        assert resp_force.status_code == 200
        assert marks == ["pnl-demo"]
