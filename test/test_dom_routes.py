#!/usr/bin/env python3
"""DOM API route tests.

This suite checks that DOM-related backend routes behave
according to design. It relies on a running instance of
our FastAPI application on http://localhost:8000.
"""

import os
from typing import Optional

import pytest
import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change_me")


def login(username: str, password: str) -> Optional[str]:
    try:
        resp = requests.post(
            f"{BASE_URL}/api/login",
            json={"username": username, "password": password},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        return resp.json().get("access_token")
    except Exception:
        return None


def api_get(path: str, token: Optional[str]):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return requests.get(f"{BASE_URL}{path}", headers=headers, timeout=10)


def api_post(path: str, token: Optional[str], json: dict):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return requests.post(f"{BASE_URL}{path}", headers=headers, json=json, timeout=15)


def api_put(path: str, token: Optional[str], json: dict):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return requests.put(f"{BASE_URL}{path}", headers=headers, json=json, timeout=15)


@pytest.fixture
def token():
    t = login(ADMIN_USERNAME, ADMIN_PASSWORD)
    assert t is not None, "unable to obtain token"
    return t


def test_start_requires_connection(token):
    resp = api_post("/api/dom/start", token, {"symbol": "AAPL"})
    assert resp.status_code == 400
    assert "Not connected" in resp.text


def test_config_get_put(token):
    resp = api_get("/api/config/dom", token)
    assert resp.status_code == 200
    data = resp.json()
    assert "window" in data and "max_levels" in data

    new_cfg = {"window": data["window"] + 1, "max_levels": data["max_levels"]}
    resp2 = api_put("/api/config/dom", token, new_cfg)
    assert resp2.status_code == 200

    resp3 = api_get("/api/config/dom", token)
    assert resp3.status_code == 200
    data3 = resp3.json()
    assert data3["window"] == new_cfg["window"]


def test_dom_data_endpoint(token):
    resp = api_get("/api/dom/data", token)
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("success") is True
    assert "metrics" in data
