import tempfile
from fastapi.testclient import TestClient

import main
from src.database import DatabaseManager, get_db_manager

class DummyTelegram:
    def update_config(self, bot_token: str = "", chat_id: str = ""):
        pass

def override_user():
    return "test-user"

def test_module_settings_persist():
    db_file = tempfile.NamedTemporaryFile(delete=False)
    db_file.close()
    db = DatabaseManager(db_file.name)

    def override_db_manager():
        return db

    # Override dependencies and globals
    main.app.dependency_overrides[main.get_current_user] = override_user
    main.app.dependency_overrides[get_db_manager] = override_db_manager
    main.get_db_manager = override_db_manager
    main.telegram_notifier = DummyTelegram()

    client = TestClient(main.app)

    payload = {
        "chrome": {"enabled": True, "config": {}, "events": ["Order Filled"]},
        "telegram": {"enabled": True, "config": {"bot_token": "x", "chat_id": "1"}, "events": ["Risk Rule"]},
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

    resp = client.put("/api/notifications/settings", json=payload)
    assert resp.status_code == 200
    assert resp.json()["success"] is True

    resp = client.get("/api/notifications/settings")
    assert resp.status_code == 200
    data = resp.json()["settings"]
    assert "Order Filled" in data["chrome"]["events"]
    assert "Risk Rule" in data["telegram"]["events"]
    assert data["telegram"]["config"]["bot_token"] == "***"
    assert data["telegram"]["config"]["chat_id"] == "***"

    # Update again with placeholders to ensure original secrets are preserved
    payload["telegram"]["config"]["bot_token"] = "***"
    payload["telegram"]["config"]["chat_id"] = "***"

    resp = client.put("/api/notifications/settings", json=payload)
    assert resp.status_code == 200
    assert resp.json()["success"] is True

    raw_settings = db.load_notification_config()
    assert raw_settings["telegram"]["config"]["bot_token"] == "x"
    assert raw_settings["telegram"]["config"]["chat_id"] == "1"

    resp = client.get("/api/notifications/settings")
    assert resp.status_code == 200
    data = resp.json()["settings"]
    assert data["telegram"]["config"]["bot_token"] == "***"
    assert data["telegram"]["config"]["chat_id"] == "***"
