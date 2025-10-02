from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Mapping

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.routes import notifications as notifications_module
from src.routes.service_clients import NotificationServiceClient, ServiceUnavailableError


class RedisStub:
    def __init__(self) -> None:
        self.json_store: dict[str, Any] = {}
        self.string_store: dict[str, Any] = {}
        self.hash_store: dict[str, dict[str, str]] = {}
        self.set_store: set[str] = set()
        self.published: list[tuple[str, str]] = []

    class _JsonCommands:
        def __init__(self, parent: "RedisStub") -> None:
            self._parent = parent

        async def get(self, key: str) -> Any:
            return self._parent.json_store.get(key)

        async def set(self, key: str, path: str, value: Any) -> None:
            self._parent.json_store[key] = value

    def json(self) -> "RedisStub._JsonCommands":  # pragma: no cover - exercised in tests
        return RedisStub._JsonCommands(self)

    async def get(self, key: str) -> Any:
        return self.string_store.get(key)

    async def set(self, key: str, value: Any) -> bool:
        self.string_store[key] = value
        return True

    async def hset(self, key: str, *, mapping: Mapping[str, Any]) -> int:
        bucket = self.hash_store.setdefault(key, {})
        for field, value in mapping.items():
            bucket[str(field)] = str(value)
        return len(mapping)

    async def sadd(self, key: str, value: str) -> int:
        before = len(self.set_store)
        self.set_store.add(str(value))
        return len(self.set_store) - before

    async def publish(self, channel: str, message: str) -> int:
        self.published.append((channel, message))
        return len(self.published)


@pytest.fixture()
def notifications_app(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    app = FastAPI()
    app.include_router(notifications_module.router)

    redis = RedisStub()

    async def allow_user(request: Request) -> str:
        request.state.is_read_only = False
        request.state.user_role = "admin"
        request.state.username = "tester"
        return "tester"

    async def fake_ensure() -> RedisStub:
        return redis

    app.dependency_overrides[notifications_module.get_current_user] = allow_user
    monkeypatch.setattr(notifications_module, "ensure_redis", fake_ensure)
    notifications_module.set_notification_service_client(None)

    yield SimpleNamespace(app=app, redis=redis)

    app.dependency_overrides.pop(notifications_module.get_current_user, None)
    notifications_module.set_notification_service_client(NotificationServiceClient())


def test_get_notification_settings_uses_redis_store(notifications_app: SimpleNamespace) -> None:
    redis: RedisStub = notifications_app.redis
    redis.json_store[notifications_module.SETTINGS_STORE_KEY] = {
        "settings": {
            "telegram": {
                "enabled": True,
                "config": {"bot_token": "abc", "chat_id": "999"},
                "events": ["order_filled", "risk_rule"],
            },
            "email_enabled": True,
            "email_address": "alerts@example.com",
        },
        "updated_at": "2024-05-01T00:00:00Z",
    }

    with TestClient(notifications_app.app) as client:
        response = client.get("/api/notifications/settings")

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["version"] == notifications_module.SETTINGS_VERSION
    assert payload["updated_at"] == "2024-05-01T00:00:00Z"

    settings = payload["data"]
    telegram = settings["telegram"]
    assert telegram["enabled"] is True
    assert telegram["config"]["bot_token"] == "***"
    assert telegram["config"]["chat_id"] == "***"
    assert "Order Filled" in telegram["events"]
    assert settings["email_enabled"] is True
    assert settings["email_address"] == "alerts@example.com"


def test_update_notification_settings_persists_to_redis(notifications_app: SimpleNamespace) -> None:
    redis: RedisStub = notifications_app.redis
    redis.json_store[notifications_module.SETTINGS_STORE_KEY] = {
        "settings": {
            "telegram": {
                "enabled": True,
                "config": {"bot_token": "old-token", "chat_id": "123"},
                "events": ["general"],
            },
            "chrome": {"enabled": True, "events": ["order_filled"]},
        }
    }

    payload = {
        "telegram": {
            "enabled": True,
            "config": {"bot_token": "***", "chat_id": "***"},
            "events": ["General", "DOM Level 3"],
        },
        "chrome": {
            "enabled": True,
            "events": ["Order Filled", "Risk Rule"],
        },
        "email_enabled": True,
        "email_address": "ops@example.com",
    }

    with TestClient(notifications_app.app) as client:
        response = client.put("/api/notifications/settings", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["telegram"]["config"]["bot_token"] == "***"
    assert body["data"]["email_enabled"] is True

    record = redis.json_store[notifications_module.SETTINGS_STORE_KEY]
    stored_settings = record["settings"]
    assert stored_settings["telegram"]["config"]["bot_token"] == "old-token"
    assert stored_settings["telegram"]["events"] == ["general", "dom_level_3"]
    assert stored_settings["chrome"]["events"] == ["order_filled", "risk_rule"]
    assert stored_settings["email_address"] == "ops@example.com"

    config_hash = redis.hash_store[notifications_module.CONFIG_HASH_KEY]
    assert config_hash["telegram_bot_token"] == "old-token"
    assert json.loads(config_hash["chrome_events"]) == ["order_filled", "risk_rule"]
    assert redis.published[-1] == (notifications_module.CONFIG_UPDATE_CHANNEL, "notification")


def test_send_notification_returns_disabled_when_service_unavailable(
    notifications_app: SimpleNamespace,
) -> None:
    class DisabledClient(NotificationServiceClient):
        async def dispatch(self, payload: Mapping[str, Any]) -> Mapping[str, Any] | None:  # type: ignore[override]
            raise ServiceUnavailableError("notification", "offline")

        async def test(self, payload: Mapping[str, Any]) -> Mapping[str, Any] | None:  # type: ignore[override]
            raise ServiceUnavailableError("notification", "offline")

    notifications_module.set_notification_service_client(DisabledClient())

    with TestClient(notifications_app.app) as client:
        response = client.post("/api/notify", json={"message": "ping", "event_type": "test"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is False
    assert payload["message"] == "Notification service not enabled"


def test_notification_history_route_calls_service(notifications_app: SimpleNamespace) -> None:
    captured: dict[str, Any] = {}

    class HistoryClient(NotificationServiceClient):
        async def history(  # type: ignore[override]
            self,
            *,
            channel: str | None = None,
            status: str | None = None,
            since: str | None = None,
            limit: int | None = None,
        ) -> Mapping[str, Any]:
            captured["params"] = {
                "channel": channel,
                "status": status,
                "since": since,
                "limit": limit,
            }
            return {"success": True, "history": [{"channel": channel or "websocket"}]}

    notifications_module.set_notification_service_client(HistoryClient())

    with TestClient(notifications_app.app) as client:
        response = client.get(
            "/api/notifications/history",
            params={"channel": "telegram", "status": "failed", "since": "2024-06-01T00:00:00Z", "limit": 25},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["history"][0]["channel"] == "telegram"
    assert captured["params"]["limit"] == 25


def test_clear_notification_history_calls_service(notifications_app: SimpleNamespace) -> None:
    class ClearClient(NotificationServiceClient):
        async def clear_history(self) -> Mapping[str, Any]:  # type: ignore[override]
            return {"success": True, "cleared": 42}

    notifications_module.set_notification_service_client(ClearClient())

    with TestClient(notifications_app.app) as client:
        response = client.delete("/api/notifications/history")

    assert response.status_code == 200
    assert response.json()["cleared"] == 42
