"""Tests for the strategy HTTP service endpoints."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest
from fastapi.testclient import TestClient

from services.strategy.http import (
    DEFAULT_SETTINGS,
    StrategyRuntime,
    create_app,
    persist_strategy_settings,
)
import src.utils.strategy_cache as strategy_cache_module
from tests.services.redis_utils import InMemoryRedis
import tests.services.strategy.sample as sample_strategy
from src.db.mariadb import set_engine
from src.db.repositories import StrategyRepository


_CURRENT_REPOSITORY: StrategyRepository | None = None

pytestmark = pytest.mark.usefixtures("strategy_repository")


@pytest.fixture
def redis_client() -> InMemoryRedis:
    client = InMemoryRedis()
    asyncio.run(client.flushall())
    try:
        yield client
    finally:
        asyncio.run(client.flushall())
        asyncio.run(client.close())


@pytest.fixture
def strategy_repository(monkeypatch: pytest.MonkeyPatch) -> StrategyRepository:
    connection = sqlite3.connect(
        "file::memory:?cache=shared", uri=True, check_same_thread=False
    )
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE strategies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            factory TEXT NULL,
            params TEXT NULL,
            metadata TEXT NULL,
            description TEXT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            enabled_at TEXT NULL,
            disabled_at TEXT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    connection.commit()
    set_engine(lambda: connection, close=False)
    repository = StrategyRepository(logger_name="tests.strategy")
    global _CURRENT_REPOSITORY
    _CURRENT_REPOSITORY = repository
    try:
        yield repository
    finally:
        _CURRENT_REPOSITORY = None
        set_engine(None)
        connection.close()


def _build_client(
    redis: InMemoryRedis, repository: StrategyRepository | None = None
) -> TestClient:
    def redis_factory() -> InMemoryRedis:
        return redis

    data_push_calls: list[tuple[str, Mapping[str, Any]]] = []

    async def data_push_handler(name: str, state: Mapping[str, Any]) -> Mapping[str, Any]:
        snapshot = {str(k): v for k, v in state.items()}
        data_push_calls.append((name, snapshot))
        return {"status_reason": "handler", "active": snapshot.get("active", False)}

    effective_repository = repository or _CURRENT_REPOSITORY
    if effective_repository is None:
        raise RuntimeError("strategy repository fixture not initialised")

    app = create_app(
        redis_factory=redis_factory,
        health_interval=0.05,
        event_poll_interval=0.05,
        data_push_handler=data_push_handler,
        strategy_repository=effective_repository,
    )
    client = TestClient(app)
    setattr(client, "data_push_calls", data_push_calls)
    return client


def _fetch_strategy(
    repository: StrategyRepository, name: str
) -> dict[str, Any] | None:
    record = asyncio.run(repository.get_strategy(name))
    if record is None:
        return None
    try:
        return record.to_payload()
    except AttributeError:
        return dict(record)  # type: ignore[arg-type]


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


def test_seeded_strategy_available_on_start(
    redis_client: InMemoryRedis, strategy_repository: StrategyRepository
) -> None:
    asyncio.run(
        strategy_repository.upsert_strategy(
            {
                "name": "BuyTheDipStrategy",
                "factory": "tests.services.strategy.sample:factory",
                "metadata": {"file": "src/strategies/buy_the_dip_strategy.py"},
                "description": "Sample",
            }
        )
    )
    client = _build_client(redis_client, strategy_repository)

    with client:
        response = client.get("/strategy")

    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, list)
    assert any(entry.get("name") == "BuyTheDipStrategy" for entry in payload)


def test_strategy_files_listing_requires_metadata(
    redis_client: InMemoryRedis, strategy_repository: StrategyRepository, tmp_path: Path
) -> None:
    strategy_dir = tmp_path / "strategies"
    strategy_dir.mkdir()
    file_path = strategy_dir / "filesystem_alpha.py"
    file_path.write_text("print('alpha')\n", encoding="utf-8")

    settings = {**DEFAULT_SETTINGS, "strategy_path": str(strategy_dir)}
    asyncio.run(persist_strategy_settings(redis_client, settings))

    client = _build_client(redis_client, strategy_repository)

    with client:
        files_response = client.get("/strategy/files")
        assert files_response.status_code == 200
        files_payload = files_response.json()
        assert files_payload["success"] is True
        assert files_payload["files"] == []

        asyncio.run(
            strategy_repository.upsert_strategy(
                {
                    "name": "FilesystemAlpha",
                    "factory": "tests.services.strategy.sample:factory",
                    "metadata": {"file": "filesystem_alpha.py"},
                }
            )
        )

        updated_response = client.get("/strategy/files")
        assert updated_response.status_code == 200
        updated_payload = updated_response.json()
        assert updated_payload["files"] == ["filesystem_alpha.py"]

        strategies_response = client.get("/strategy")
        assert strategies_response.status_code == 200
        strategies = strategies_response.json()
        names = [entry["name"] for entry in strategies]
        assert "FilesystemAlpha" in names


def test_strategy_crud_lifecycle(
    redis_client: InMemoryRedis, strategy_repository: StrategyRepository
) -> None:
    client = _build_client(redis_client, strategy_repository)

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
        stored = _fetch_strategy(strategy_repository, "demo")
        assert stored is not None
        assert stored["name"] == "demo"
        assert stored["metadata"]["owner"] == "qa"
        assert stored["created_at"] == created["created_at"]

        list_response = client.get("/strategy")
        assert list_response.status_code == 200
        listing = {entry["name"]: entry for entry in list_response.json()}
        assert listing["demo"]["factory"] == stored["factory"]

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
        stored = _fetch_strategy(strategy_repository, "demo")
        assert stored is not None
        assert stored["params"]["symbol"] == "NQ"
        assert stored["params"]["size"] == 2
        assert stored["updated_at"] == updated["updated_at"]

        response = client.delete("/strategy/demo")
        assert response.status_code == 204

    removed = _fetch_strategy(strategy_repository, "demo")
    assert removed is None


def test_internal_info_endpoint_refreshes(redis_client: InMemoryRedis) -> None:
    client = _build_client(redis_client)
    payload = {
        "name": "inspector",
        "factory": "tests.services.strategy.sample:factory",
        "params": {"symbol": "ES", "size": 2},
        "description": "Inspector",
    }

    sample_strategy.set_internal_info_failure(False)
    try:
        with client:
            assert client.post("/strategies", json=payload).status_code == 201

            response = client.get("/strategies/inspector/internal-info")
            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            assert data["cached"] is False
            info = data["strategy"]
            assert info["description"] == "Inspector"
            assert any(entry["name"] == "size" for entry in info["configurable_parameters"])

            sample_strategy.set_internal_info_failure(True)
            cached_response = client.get("/strategies/inspector/internal-info")
            assert cached_response.status_code == 200
            cached_payload = cached_response.json()
            assert cached_payload["cached"] is True
            assert cached_payload["strategy"]["stale"] is True

            sample_strategy.set_internal_info_failure(False)
            refreshed_response = client.get("/strategies/inspector/internal-info")
            assert refreshed_response.status_code == 200
            refreshed_payload = refreshed_response.json()
            assert refreshed_payload["cached"] is False
            assert refreshed_payload["strategy"].get("stale") is not True
    finally:
        sample_strategy.set_internal_info_failure(False)


def test_internal_parameter_update_persists_value(redis_client: InMemoryRedis) -> None:
    client = _build_client(redis_client)
    payload = {
        "name": "configurable",
        "factory": "tests.services.strategy.sample:factory",
        "params": {"symbol": "CL", "size": 1},
    }

    with client:
        assert client.post("/strategies", json=payload).status_code == 201

        response = client.post(
            "/strategies/configurable/internal-info/parameter",
            json={"name": "size", "value": 3},
        )
        assert response.status_code == 200
        parameter = response.json()["parameter"]
        assert parameter["name"] == "size"
        assert parameter["value"] == 3

        overrides_key = "json:strategy:configurable:parameters"
        stored = asyncio.run(redis_client.get(overrides_key))
        assert stored is not None
        overrides = json.loads(stored)
        assert overrides["size"] == 3

        bad_response = client.post(
            "/strategies/configurable/internal-info/parameter",
            json={"name": "size", "value": 0},
        )
        assert bad_response.status_code == 400


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


def test_strategy_daily_pnl_endpoint(redis_client: InMemoryRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _build_client(redis_client)
    payload = {
        "name": "pnl-demo",
        "factory": "tests.services.strategy.sample:factory",
        "params": {"symbol": "GC"},
    }

    async def fake_daily(name: str) -> list[dict[str, Any]]:
        return [{"date": "2024-01-01", "pnl": 12.5}]

    mark_calls: list[str] = []

    async def fake_mark(name: str) -> None:
        mark_calls.append(name)

    monkeypatch.setattr("services.strategy.http.get_daily_pnl", fake_daily)
    monkeypatch.setattr("services.strategy.http.mark_strategy_cache_stale", fake_mark)

    with client:
        assert client.post("/strategies", json=payload).status_code == 201
        response = client.get("/strategies/pnl-demo/pnl/daily")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["pnl"][0]["pnl"] == 12.5

        force_resp = client.get("/strategies/pnl-demo/pnl/daily", params={"force_refresh": "true"})
        assert force_resp.status_code == 200
        assert mark_calls == ["pnl-demo"]


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


def test_strategy_toggle_aliases(
    redis_client: InMemoryRedis, strategy_repository: StrategyRepository
) -> None:
    client = _build_client(redis_client, strategy_repository)

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

        stored = _fetch_strategy(strategy_repository, "switch")
        assert stored is not None
        assert stored["enabled"] is False

        toggle_resp = client.post("/strategy/switch/toggle", json={"enabled": True})
        assert toggle_resp.status_code == 200
        data = toggle_resp.json()
        assert data["strategy"]["enabled"] is True

        stored = _fetch_strategy(strategy_repository, "switch")
        assert stored is not None
        assert stored["enabled"] is True

        bad_resp = client.post("/strategy/switch/toggle")
        assert bad_resp.status_code == 400


def test_strategy_risk_settings_round_trip(redis_client: InMemoryRedis) -> None:
    client = _build_client(redis_client)
    payload = {
        "name": "risk-demo",
        "factory": "tests.services.strategy.sample:factory",
        "params": {"symbol": "ES"},
    }

    with client:
        assert client.post("/strategies", json=payload).status_code == 201

        response = client.get("/strategies/risk-demo/risk-settings")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["settings"]["forbid_add_on_loss"] is True

        update_payload = {
            "max_positions": 4,
            "loss_time_threshold": 120,
            "loss_pnl_threshold": 250.5,
            "notify_on_trigger": False,
        }
        update_resp = client.put("/strategies/risk-demo/risk-settings", json=update_payload)
        assert update_resp.status_code == 200
        updated = update_resp.json()["settings"]
        assert updated["max_positions"] == 4
        assert updated["notify_on_trigger"] is False

    stored = asyncio.run(redis_client.get("json:strategy:risk-demo:risk-settings"))
    assert stored is not None
    payload = json.loads(stored)
    assert payload["max_positions"] == 4
    assert payload["loss_time_threshold"] == 120
    assert payload["notify_on_trigger"] is False


def test_strategy_status_and_data_push_updates_state(redis_client: InMemoryRedis) -> None:
    client = _build_client(redis_client)
    payload = {
        "name": "status-demo",
        "factory": "tests.services.strategy.sample:factory",
        "params": {"symbol": "NQ"},
    }

    with client:
        assert client.post("/strategies", json=payload).status_code == 201

        status_resp = client.get("/strategies/status-demo/status")
        assert status_resp.status_code == 200
        status = status_resp.json()["status"]
        assert status["pending_tasks"] == 0
        assert status["data_push"] == {}

        data_resp = client.post(
            "/strategies/status-demo/data-push",
            json={"enabled": True, "subscription": "market_data", "symbol": "NQ"},
        )
        assert data_resp.status_code == 200
        state = data_resp.json()["state"]
        assert state["active"] is True
        assert state["subscription"] == "market_data"

        calls = getattr(client, "data_push_calls", [])
        assert calls and calls[-1][0] == "status-demo"
        assert calls[-1][1]["active"] is True

        refreshed = client.get("/strategies/status-demo/status")
        assert refreshed.status_code == 200
        refreshed_state = refreshed.json()["status"]["data_push"]
        assert refreshed_state["active"] is True
        assert refreshed_state["status_reason"] == "handler"


def test_strategy_reset_aliases(
    redis_client: InMemoryRedis, strategy_repository: StrategyRepository
) -> None:
    client = _build_client(redis_client, strategy_repository)

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

        stored = _fetch_strategy(strategy_repository, "resetme")
        assert stored is not None
        assert stored["enabled"] is True
        assert stored["params"] == {}
        assert stored["metadata"] == {}

        legacy_resp = client.post("/strategy/resetme/reset")
        assert legacy_resp.status_code == 200

    missing_resp = client.post("/strategy/unknown/reset")
    assert missing_resp.status_code == 404


def test_strategy_rename_endpoint_rewrites_metadata(
    redis_client: InMemoryRedis, strategy_repository: StrategyRepository
) -> None:
    client = _build_client(redis_client, strategy_repository)
    payload = {
        "name": "rename-me",
        "factory": "tests.services.strategy.sample:factory",
        "params": {"symbol": "YM"},
    }

    with client:
        assert client.post("/strategies", json=payload).status_code == 201

        risk_payload = {
            "max_positions": 2,
            "notify_on_trigger": False,
        }
        assert (
            client.put("/strategies/rename-me/risk-settings", json=risk_payload).status_code
            == 200
        )

        rename_resp = client.post("/strategies/rename-me/rename", json={"new_name": "renamed"})
        assert rename_resp.status_code == 200
        renamed = rename_resp.json()["strategy"]
        assert renamed["name"] == "renamed"

    assert asyncio.run(redis_client.get("json:strategy:rename-me")) is None
    renamed_meta = _fetch_strategy(strategy_repository, "renamed")
    assert renamed_meta is not None
    assert renamed_meta["name"] == "renamed"
    risk_state = asyncio.run(redis_client.get("json:strategy:renamed:risk-settings"))
    assert risk_state is not None
    assert json.loads(risk_state)["max_positions"] == 2


def test_strategy_files_endpoint_returns_metadata_files(
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
        "metadata": {
            "files": ["legacy.py", "nested/beta.py", "legacy.py"],
            "scripts": {"primary": "alpha.py"},
        },
    }

    with client:
        assert client.post("/strategies", json=payload).status_code == 201

        response = client.get("/strategy/files")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        files = data["files"]
        assert files == sorted(["alpha.py", "legacy.py", "nested/beta.py"])

        (strategy_root / "gamma.py").write_text("# gamma\n")
        response = client.get("/strategy/files")
        files = response.json()["files"]
        assert "gamma.py" not in files


def test_strategy_filesystem_changes_do_not_autoregister(
    redis_client: InMemoryRedis, tmp_path: Path
) -> None:
    strategy_dir = tmp_path / "strategies"
    strategy_dir.mkdir()

    settings = {**DEFAULT_SETTINGS, "strategy_path": str(strategy_dir)}
    asyncio.run(persist_strategy_settings(redis_client, settings))

    client = _build_client(redis_client)

    with client:
        response = client.get("/strategy/files")
        assert response.status_code == 200
        initial_files = response.json()["files"]
        assert initial_files == []

        file_path = strategy_dir / "omega.py"
        file_path.write_text("print('omega')\n", encoding="utf-8")

        files_response = client.get("/strategy/files")
        assert files_response.status_code == 200
        files_payload = files_response.json()
        assert files_payload["files"] == []

        strategies_response = client.get("/strategy")
        assert strategies_response.status_code == 200
        strategies = strategies_response.json()
        assert strategies == []


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


def test_external_strategy_order_submission(redis_client: InMemoryRedis) -> None:
    client = _build_client(redis_client)
    payload = {
        "strategy": "external-alpha",
        "symbol": "ES",
        "action": "BUY",
        "qty": 1,
        "type": "MARKET",
    }

    with client:
        response = client.post("/strategy", json=payload)
        assert response.status_code == 202
        body = response.json()
        assert body["success"] is True
        assert body["order"]["strategy"] == "external-alpha"

    queued_items = asyncio.run(redis_client.lrange("list:queue:strategy", 0, 0))
    assert queued_items
    order_payload = json.loads(queued_items[0])
    assert order_payload["strategy"] == "external-alpha"
    assert order_payload["symbol"] == "ES"
    metadata = order_payload.get("metadata", {})
    assert metadata.get("client") is not None


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
