"""Tests for the strategy HTTP service endpoints."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from services.strategy.discovery import sync_strategy_metadata
from services.strategy.http import StrategyRuntime, create_app
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
