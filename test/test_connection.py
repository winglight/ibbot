from http import HTTPStatus


def test_get_connection_config(client):
    response = client.get("/api/connection_config")
    assert response.status_code == HTTPStatus.OK
    data = response.json()
    assert set(data.keys()) == {"host", "port", "client_id", "paper_trading"}


def test_connection_status(client, admin_headers):
    response = client.get("/api/connection_status", headers=admin_headers)
    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert "connected" in payload and isinstance(payload["connected"], bool)
    assert payload["account_type"] in {"Live", "Paper"}


def test_connection_status_dropped_connection(client, admin_headers, monkeypatch):
    calls = {"is_connected": 0}

    def fake_is_connected():
        calls["is_connected"] += 1
        raise RuntimeError("client unreachable")

    monkeypatch.setattr(
        "core.connection_manager.is_connected",
        fake_is_connected,
        raising=True,
    )
    monkeypatch.setattr(
        "core.connection_manager.get_last_connect_error",
        lambda: "connection lost",
        raising=True,
    )

    response = client.get("/api/connection_status", headers=admin_headers)
    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["connected"] is False
    assert payload["last_error"] == "connection lost"
    assert calls["is_connected"] == 1
