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
