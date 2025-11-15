import os
from http import HTTPStatus


def test_admin_login(client):
    response = client.post(
        "/api/login",
        json={"username": os.environ["ADMIN_USERNAME"], "password": os.environ["ADMIN_PASSWORD"]},
        
    )
    assert response.status_code == HTTPStatus.OK
    data = response.json()
    assert data["role"] == "admin"
    assert data["is_read_only"] is False
    assert "access_token" in data and data["token_type"] == "bearer"


def test_visitor_read_only(client, visitor_headers):
    response = client.put(
        "/api/config/dom",
        json={"window": 5000, "max_levels": 20, "trend_thresholds": [0.5, 0.7]},
        headers=visitor_headers,
        
    )
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert response.json()["detail"] == "Read-only access"
