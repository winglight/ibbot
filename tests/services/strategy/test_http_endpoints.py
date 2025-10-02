"""Tests for the strategy HTTP service endpoints."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.strategy.discovery import sync_strategy_metadata
from services.strategy.http import (
    DEFAULT_SETTINGS,
    StrategyRuntime,
    create_app,
    persist_strategy_settings,
)
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


def _build_client(redis: InMemoryRedis) -> TestClient:
    def redis_factory() -> InMemoryRedis:
        return redis

    app = create_app(
        redis_factory=redis_factory,
        health_interval=0.05,
        event_poll_interval=0.05,
    )
    return TestClient(app)


def _read_metadata(redis: InMemoryRedis, name: str) -> dict[str, object]:
    raw = asyncio.run(redis.get(f"json:strategy:{name}"))
    if not raw:
        return {}
    if isinstance(raw, str):
        return json.loads(raw)
    return dict(raw)


def test_discovered_strategy_available_on_start(redis_client: InMemoryRedis) -> None:
    asyncio.run(sync_strategy_metadata(redis_client, {}))
    client = _build_client(redis_client)

    with client:
        response = client.get("/strategy")

    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, list)
    assert any(entry.get("name") == "BuyTheDipStrategy" for entry in payload)


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
        assert response.json() == []

        response = client.post("/strategy", json=payload)
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
        assert client.post("/strategy", json=payload).status_code == 201
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
        assert client.post("/strategy", json=payload).status_code == 201

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
        assert client.post("/strategy", json=payload).status_code == 201

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
        assert client.post("/strategy", json=payload).status_code == 201

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
