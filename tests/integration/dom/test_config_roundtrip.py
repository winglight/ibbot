"""Integration tests for DOM configuration endpoints using the real app."""

from __future__ import annotations

from copy import deepcopy
from http import HTTPStatus
from typing import Dict

from fastapi.testclient import TestClient


def test_dom_config_roundtrip(client: TestClient, admin_headers: Dict[str, str]) -> None:
    response = client.get("/api/config/dom", headers=admin_headers)
    assert response.status_code == HTTPStatus.OK
    original = response.json()

    updated = deepcopy(original)
    updated["window"] = original["window"] // 2 or 1000
    updated["max_levels"] = max(5, original["max_levels"] - 1)
    updated["trend_thresholds"] = [0.4, 0.6, 0.8]

    try:
        put_resp = client.put("/api/config/dom", headers=admin_headers, json=updated)
        assert put_resp.status_code == HTTPStatus.OK
        payload = put_resp.json()
        assert payload["success"] is True
        assert payload["config"]["window"] == updated["window"]
        returned = payload["config"]["trend_thresholds"]
        assert returned[: len(updated["trend_thresholds"])] == updated["trend_thresholds"]
    finally:
        client.put("/api/config/dom", headers=admin_headers, json=original)
