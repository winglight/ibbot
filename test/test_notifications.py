from http import HTTPStatus
from types import SimpleNamespace

import pytest


@pytest.fixture
def notification_test_env(monkeypatch, app):
    class StubDB:
        def __init__(self):
            self.saved = None
            self.history_filters = None
            self.cleared = False

        def load_notification_config(self):
            return {
                "chrome": {
                    "enabled": True,
                    "config": {},
                    "events": ["order_filled", "risk_rule"],
                },
                "telegram": {
                    "enabled": True,
                    "config": {"bot_token": "old-token", "chat_id": "123"},
                    "events": ["general"],
                },
                "trading_reminder": {
                    "enabled": False,
                    "start_trading_time": "09:30",
                    "end_trading_time": "16:00",
                    "browser_notification_enabled": True,
                    "telegram_reminder_enabled": False,
                },
                "email_enabled": False,
                "email_address": "",
            }

        def save_notification_config(self, payload):
            self.saved = payload

        def load_notification_history(self, **filters):
            self.history_filters = filters
            return [
                {
                    "id": 1,
                    "channel": filters.get("channel", "websocket"),
                    "status": filters.get("status", "sent"),
                }
            ]

        def clear_notification_history(self):
            self.cleared = True
            return 5

    class StubNotifier:
        def __init__(self):
            self.updated = []

        def update_config(self, *, bot_token: str, chat_id: str):
            self.updated.append((bot_token, chat_id))

    class StubManager:
        def __init__(self):
            self.calls = []

        async def send_notification(self, **kwargs):  # type: ignore[override]
            self.calls.append(kwargs)
            channels = kwargs.get("channels")
            if isinstance(channels, list):
                return {channel: True for channel in channels}
            return {"websocket": True}

    from src.routes import notifications as notifications_module

    stub_db = StubDB()
    stub_manager = StubManager()
    stub_notifier = StubNotifier()

    app.dependency_overrides[notifications_module.get_db_manager] = lambda: stub_db
    monkeypatch.setattr(notifications_module, "notification_manager", stub_manager)
    monkeypatch.setattr(notifications_module, "telegram_notifier", stub_notifier)

    yield SimpleNamespace(db=stub_db, manager=stub_manager, notifier=stub_notifier)

    app.dependency_overrides.pop(notifications_module.get_db_manager, None)


def test_notification_settings_sanitized(client, admin_headers):
    response = client.get("/api/notifications/settings", headers=admin_headers)
    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["success"] is True
    settings = payload["settings"]

    assert "chrome" in settings and "telegram" in settings
    chrome = settings["chrome"]
    telegram = settings["telegram"]

    assert isinstance(chrome["events"], list) and len(chrome["events"]) > 0
    assert isinstance(telegram["events"], list)
    assert set(telegram["config"].keys()) == {"bot_token", "chat_id"}
    # 敏感信息在未配置时为空，若存在则使用 *** 掩码
    assert telegram["config"]["bot_token"] in {"", "***"}
    assert telegram["config"]["chat_id"] in {"", "***"}


def test_send_notification_returns_success_flag(client, admin_headers):
    response = client.post(
        "/api/notify",
        headers=admin_headers,
        json={"message": "Test notification", "event_type": "test"},
        
    )
    assert response.status_code == HTTPStatus.OK
    data = response.json()
    assert "success" in data


def test_update_notification_settings_overrides(  # type: ignore[unused-ignore]
    client,
    admin_headers,
    notification_test_env,
):
    payload = {
        "chrome": {
            "enabled": True,
            "config": {},
            "events": ["Order Filled", "Risk Rule"],
        },
        "telegram": {
            "enabled": True,
            "config": {"bot_token": "***", "chat_id": "***"},
            "events": ["General", "DOM Level 3"],
        },
    }

    response = client.put(
        "/api/notifications/settings",
        headers=admin_headers,
        json=payload,
    )
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["success"] is True

    saved = notification_test_env.db.saved
    assert saved is not None
    assert saved["chrome"]["events"] == ["order_filled", "risk_rule"]
    assert saved["telegram"]["config"]["bot_token"] == "old-token"
    assert saved["telegram"]["config"]["chat_id"] == "123"

    assert notification_test_env.notifier.updated[-1] == ("old-token", "123")


def test_notification_history_and_clear(client, admin_headers, notification_test_env):
    response = client.get(
        "/api/notifications/history",
        headers=admin_headers,
        params={"channel": "telegram", "status": "sent", "limit": 10},
    )
    assert response.status_code == HTTPStatus.OK
    data = response.json()
    assert data["success"] is True
    assert data["history"]

    filters = notification_test_env.db.history_filters
    assert filters == {"channel": "telegram", "status": "sent", "since": None, "limit": 10}

    clear_resp = client.delete("/api/notifications/history", headers=admin_headers)
    assert clear_resp.status_code == HTTPStatus.OK
    clear_body = clear_resp.json()
    assert clear_body == {"success": True, "cleared": 5}
    assert notification_test_env.db.cleared is True


def test_notification_test_endpoint_maps_channels(
    client,
    admin_headers,
    notification_test_env,
):
    response = client.post(
        "/api/notifications/test",
        headers=admin_headers,
        json={"channel": "chrome", "message": "ping"},
    )
    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["success"] is True

    call = notification_test_env.manager.calls[-1]
    assert call["channels"] == ["websocket"]
    assert call["event_type"] == "test"
