from http import HTTPStatus


def _service_slugs(payload):
    services = payload.get("services") or []
    return {item.get("slug") for item in services if isinstance(item, dict)}


def test_list_services_includes_notification_module(client, admin_headers):
    response = client.get("/api/services", headers=admin_headers)
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    slugs = _service_slugs(body)
    assert "notification_manager" in slugs

    notification_entry = next(
        (item for item in body["services"] if item.get("slug") == "notification_manager"),
        None,
    )
    assert notification_entry is not None
    assert notification_entry.get("name") == "Notification Service"


def test_service_health_reports_notification_manager(client, admin_headers):
    response = client.get("/api/services/health", headers=admin_headers)
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    slugs = _service_slugs(body)
    assert "notification_manager" in slugs


def test_restart_notification_manager(client, admin_headers):
    response = client.post(
        "/api/services/notification_manager/restart",
        headers=admin_headers,
    )
    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload.get("success") is True
    assert payload.get("module") == "notification_manager"
