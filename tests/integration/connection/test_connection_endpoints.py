"""Integration tests for the connection configuration and status endpoints."""

from __future__ import annotations

from http import HTTPStatus
from typing import Dict

from fastapi.testclient import TestClient


def test_connection_config_returns_environment_defaults(
    client: TestClient,
    expected_connection_config: Dict[str, int | str | bool],
) -> None:
    """The connection config endpoint should surface the normalised environment values."""

    response = client.get("/api/connection_config")

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload == expected_connection_config


def test_connection_status_falls_back_to_http_payload(
    client: TestClient,
    admin_headers: Dict[str, str],
) -> None:
    """When Redis has no health payload the endpoint should return the HTTP fallback."""

    response = client.get("/api/connection_status", headers=admin_headers)

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["source"] == "http"
    assert payload["connected"] is False
    assert payload["account_type"] == "Paper"
    assert payload["queue_depth"] is None
    assert payload["last_seen"] is None
    assert payload["error_code"] is None
    assert payload["last_error"] == "ib-command health unavailable"
    assert payload["stale"] is True
