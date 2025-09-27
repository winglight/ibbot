from http import HTTPStatus


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
