import os
import os
import sys
import time
import subprocess
from typing import Optional

import pytest
import requests


def _base_url() -> str:
    return os.getenv("API_BASE_URL", "http://localhost:8000")


def _is_server_up() -> bool:
    try:
        r = requests.get(f"{_base_url()}/api-docs.json", timeout=2)
        return r.status_code in (200, 404)
    except Exception:
        return False


@pytest.fixture(scope="session", autouse=True)
def server():
    """Ensure FastAPI server is running on localhost:8000 for API tests.
    If not running, start a uvicorn subprocess and stop it after the session.
    """
    if os.getenv("IBBOT_SKIP_SERVER", "0") == "1":
        yield
        return
    if _is_server_up():
        # Assume external server provided
        yield
        return

    env = os.environ.copy()
    env.setdefault("ADMIN_USERNAME", "admin")
    env.setdefault("ADMIN_PASSWORD", "change_me")

    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "main:app",
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
        "--log-level",
        "warning",
    ]
    proc = subprocess.Popen(cmd, env=env)

    # Wait for server to be ready
    for _ in range(80):  # up to ~40s
        if _is_server_up():
            break
        time.sleep(0.5)
    else:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        pytest.fail("Failed to start local test server on http://localhost:8000")

    yield

    # Teardown server if we started it
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _login(username: str, password: str) -> Optional[str]:
    try:
        resp = requests.post(
            f"{_base_url()}/api/login",
            json={"username": username, "password": password},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        return resp.json().get("access_token")
    except Exception:
        return None


@pytest.fixture(scope="session")
def token(server) -> str:
    """Session-scoped auth token for tests that require Authorization header."""
    username = os.getenv("ADMIN_USERNAME", "admin")
    password = os.getenv("ADMIN_PASSWORD", "change_me")

    # Retry a few times in case server just started
    for _ in range(10):
        tok = _login(username, password)
        if tok:
            return tok
        time.sleep(0.5)
    pytest.fail("无法获取认证令牌: /api/login 未返回有效 token")