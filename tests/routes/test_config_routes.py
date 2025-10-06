from __future__ import annotations

from typing import Any, Mapping
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, Request, status
from fastapi.testclient import TestClient

from src.routes import config as config_module


class MockRedis:
    """Mock Redis client for testing."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.sets: dict[str, set[str]] = {}
        self.lists: dict[str, list[str]] = {}
        self.hashes: dict[str, dict[str, str]] = {}

    async def smembers(self, key: str) -> set[str]:
        return self.sets.get(key, set())

    async def hgetall(self, key: str) -> dict[str, str]:
        return self.hashes.get(key, {})

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        return self.lists.get(key, [])

    async def hmset(self, key: str, mapping: dict[str, str]) -> bool:
        if key not in self.hashes:
            self.hashes[key] = {}
        self.hashes[key].update(mapping)
        return True

    async def sadd(self, key: str, *values: str) -> int:
        if key not in self.sets:
            self.sets[key] = set()
        self.sets[key].update(values)
        return len(values)

    async def lpush(self, key: str, *values: str) -> int:
        if key not in self.lists:
            self.lists[key] = []
        self.lists[key] = list(values) + self.lists[key]
        return len(self.lists[key])

    async def ltrim(self, key: str, start: int, end: int) -> bool:
        if key in self.lists:
            self.lists[key] = self.lists[key][start:end+1]
        return True

    async def publish(self, channel: str, message: str) -> int:
        return 1


@pytest.fixture()
def config_app(monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, MockRedis]:
    """Create a test FastAPI app with mocked dependencies."""
    app = FastAPI()
    app.include_router(config_module.router)

    async def allow_user(request: Request) -> str:
        request.state.is_read_only = False
        request.state.user_role = "admin"
        request.state.username = "tester"
        return "tester"

    app.dependency_overrides[config_module.get_current_user] = allow_user

    mock_redis = MockRedis()
    monkeypatch.setattr(config_module, "ensure_redis", lambda: mock_redis)

    return app, mock_redis


def test_list_modules_returns_module_list(config_app: tuple[FastAPI, MockRedis]) -> None:
    """Test that /api/config/modules returns a list of modules."""
    app, mock_redis = config_app
    mock_redis.sets["set:config:modules"] = {"risk", "strategy", "market_data"}

    client = TestClient(app)
    response = client.get("/api/config/modules")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert "modules" in data
    assert set(data["modules"]) == {"risk", "strategy", "market_data"}


def test_read_module_returns_config(config_app: tuple[FastAPI, MockRedis]) -> None:
    """Test that /api/config/{module} returns module configuration."""
    app, mock_redis = config_app
    mock_redis.hashes["hash:config:risk"] = {
        "max_position_size": "1000",
        "stop_loss_pct": "0.02"
    }

    client = TestClient(app)
    response = client.get("/api/config/risk")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert "config" in data
    assert data["config"]["max_position_size"] == "1000"
    assert data["config"]["stop_loss_pct"] == "0.02"


def test_read_history_returns_history(config_app: tuple[FastAPI, MockRedis]) -> None:
    """Test that /api/config/{module}/history returns configuration history."""
    app, mock_redis = config_app
    mock_redis.lists["list:config-history:risk"] = [
        '{"timestamp": "2023-01-01T00:00:00Z", "values": {"max_position_size": "1000"}}'
    ]

    client = TestClient(app)
    response = client.get("/api/config/risk/history")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert "history" in data
    assert len(data["history"]) == 1


def test_update_module_updates_config(config_app: tuple[FastAPI, MockRedis]) -> None:
    """Test that PUT /api/config/{module} updates module configuration."""
    app, mock_redis = config_app

    client = TestClient(app)
    payload = {
        "values": {"max_position_size": "2000"},
        "message": "Updated position size"
    }
    response = client.put("/api/config/risk", json=payload)

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["success"] is True
    assert "hash:config:risk" in mock_redis.hashes
    assert mock_redis.hashes["hash:config:risk"]["max_position_size"] == "2000"


def test_rollback_module_restores_config(config_app: tuple[FastAPI, MockRedis]) -> None:
    """Test that POST /api/config/{module}/rollback restores previous configuration."""
    app, mock_redis = config_app
    
    # Setup current config
    mock_redis.hashes["hash:config:risk"] = {"max_position_size": "2000"}
    
    # Setup history with previous version
    mock_redis.lists["list:config-history:risk"] = [
        '{"version": "v1", "timestamp": "2023-01-01T00:00:00Z", "values": {"max_position_size": "1000"}}'
    ]

    client = TestClient(app)
    payload = {
        "version": "v1",
        "message": "Rolling back to previous version"
    }
    response = client.post("/api/config/risk/rollback", json=payload)

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["success"] is True


def test_non_api_routes_work(config_app: tuple[FastAPI, MockRedis]) -> None:
    """Test that non-API routes (without /api prefix) work correctly."""
    app, mock_redis = config_app
    mock_redis.sets["set:config:modules"] = {"risk"}

    client = TestClient(app)
    response = client.get("/config/modules")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert "modules" in data
    assert "risk" in data["modules"]