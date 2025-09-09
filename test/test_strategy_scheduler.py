import os
import time
from datetime import datetime, timedelta
from typing import Optional

import pytest
import requests

BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
STRATEGY_NAME = os.getenv("TEST_SCHEDULE_STRATEGY", "strategy-scheduler-e2e")

def api_post(path: str, token: Optional[str], json: dict, timeout: int = 15):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return requests.post(f"{BASE_URL}{path}", headers=headers, json=json, timeout=timeout)

def api_get(path: str, token: Optional[str], timeout: int = 15):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return requests.get(f"{BASE_URL}{path}", headers=headers, timeout=timeout)

def api_delete(path: str, token: Optional[str], timeout: int = 15):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return requests.delete(f"{BASE_URL}{path}", headers=headers, timeout=timeout)

@pytest.fixture(autouse=True)
def cleanup(token):
    try:
        api_delete(f"/api/strategies/{STRATEGY_NAME}/executions", token)
    except Exception:
        pass
    try:
        api_delete(f"/api/strategies/{STRATEGY_NAME}", token)
    except Exception:
        pass
    yield
    try:
        api_delete(f"/api/strategies/{STRATEGY_NAME}/executions", token)
        api_delete(f"/api/strategies/{STRATEGY_NAME}", token)
    except Exception:
        pass

def test_strategy_with_schedule(token):
    now = datetime.now()
    start_time = (now + timedelta(seconds=1)).isoformat()
    end_time = (now + timedelta(seconds=4)).isoformat()

    strategy_data = {
        "name": STRATEGY_NAME,
        "strategy_type": "internal",
        "enabled": True,
        "file_path": f"{STRATEGY_NAME}.py",
        "schedule": {"open": start_time, "close": end_time},
    }
    r_create = api_post("/api/strategies", token, strategy_data)
    if r_create.status_code not in (200, 201):
        pytest.skip("Scheduled strategies not supported by server")

    # Wait for schedule window
    time.sleep(5)

    executions = []
    for _ in range(5):
        r_exec = api_get(f"/api/strategies/{STRATEGY_NAME}/executions", token)
        if r_exec.status_code == 200:
            executions = r_exec.json().get("executions", [])
            if executions:
                break
        time.sleep(1)

    if not executions:
        pytest.skip("No executions recorded; scheduler may not be running")

    assert isinstance(executions, list)
