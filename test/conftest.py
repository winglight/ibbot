import os
import pytest
import requests
from dotenv import load_dotenv
from typing import Optional
import time
import subprocess
import signal
import socket

# Ensure environment variables from .env are loaded for tests
load_dotenv()

BASE_URL = os.getenv("API_BASE_URL", os.getenv("TEST_BASE_URL", "http://localhost:8000"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change_me")
DEFAULT_SYMBOL = os.getenv("TEST_SYMBOL", "AAPL")
API_HOST = os.getenv("API_HOST", "127.0.0.1")
API_PORT = int(os.getenv("API_PORT", "8000"))


def _server_healthy(url: str) -> bool:
    try:
        r = requests.post(f"{url}/api/login", json={"username": "__health__", "password": "__health__"}, timeout=2)
        # 401 或 200 都代表服务可用
        if r.status_code in (200, 401):
            return True
    except Exception:
        pass
    try:
        r = requests.get(f"{url}/api-docs.json", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def _find_free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


# 若默认 BASE_URL 不健康，则动态分配端口并覆盖环境变量，确保后续测试使用可用地址
_base_url_stripped = BASE_URL.rstrip('/')
if not _server_healthy(_base_url_stripped):
    try:
        free_port = _find_free_port(API_HOST)
        os.environ['API_PORT'] = str(free_port)
        os.environ['API_BASE_URL'] = f"http://{API_HOST}:{free_port}"
        # 更新模块变量，供本文件后续使用
        API_PORT = free_port
        BASE_URL = os.environ['API_BASE_URL']
    except Exception:
        # 忽略异常，保持原设置，后续ensure_server_running再尝试启动
        pass


@pytest.fixture(scope="session", autouse=True)
def ensure_server_running():
    """确保在测试会话期间 FastAPI 服务处于运行状态。若未运行，则临时启动。"""
    base_url = BASE_URL.rstrip('/')

    # 已在运行则直接继续
    if _server_healthy(base_url):
        yield
        return

    # 未运行则启动 uvicorn main:app
    env = os.environ.copy()
    # 测试会话隔离数据库：若未显式指定，设置 DB_PATH 指向临时测试数据库
    if 'DB_PATH' not in env and 'TRADING_BOT_DB_PATH' not in env:
        try:
            test_db_dir = os.path.join(os.getcwd(), ".pytest_tmp_db")
            os.makedirs(test_db_dir, exist_ok=True)
            env['DB_PATH'] = os.path.join(test_db_dir, f"test_{API_PORT}.db")
        except Exception:
            pass
    cmd = [
        'uvicorn', 'main:app',
        '--host', API_HOST,
        '--port', str(API_PORT),
        '--log-level', os.getenv('UVICORN_LOG_LEVEL', 'warning'),
        '--timeout-keep-alive', '5',
    ]

    proc: Optional[subprocess.Popen] = None
    try:
        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        # 等待服务就绪
        deadline = time.time() + 30
        while time.time() < deadline:
            if _server_healthy(base_url):
                break
            if proc.poll() is not None:
                out = ''
                try:
                    out = proc.stdout.read().decode(errors='ignore') if proc.stdout else ''
                except Exception:
                    pass
                raise RuntimeError(f"启动 uvicorn 失败，退出码={proc.returncode}, 输出=\n{out}")
            time.sleep(0.3)
        else:
            raise TimeoutError("等待 uvicorn 启动超时 (30s)")
        yield
    finally:
        # 测试结束后关闭服务（仅关闭由我们启动的进程）
        if 'proc' in locals() and proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass
            deadline = time.time() + 5
            while time.time() < deadline and proc.poll() is None:
                time.sleep(0.1)
            if proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass


@pytest.fixture(scope="session")
def token() -> str:
    """Obtain a JWT token by logging into the running FastAPI service."""
    resp = requests.post(
        f"{BASE_URL}/api/login",
        headers={"Content-Type": "application/json"},
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        timeout=10,
    )
    assert resp.status_code == 200, f"Login failed: {resp.status_code} {resp.text}"
    data = resp.json()
    assert "access_token" in data and isinstance(data["access_token"], str)
    return data["access_token"]


@pytest.fixture(scope="session")
def order_id(token: str) -> Optional[int]:
    """Create a limit order and return its ID for cancellation tests.
    Returns None if not connected or if order creation fails.
    """
    # Check connection status first
    conn_resp = requests.get(
        f"{BASE_URL}/api/connection_status",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    if conn_resp.status_code != 200:
        return None
    
    conn_data = conn_resp.json()
    if not conn_data.get("connected"):
        return None
    
    # Try to place a limit order far from market price
    payload = {
        "symbol": DEFAULT_SYMBOL,
        "quantity": 1,
        "price": 1.0,  # Far from market price to avoid execution
        "action": "BUY",
        "order_type": "LIMIT",
        "exchange": "SMART",
        "sec_type": "STK",
    }
    
    resp = requests.post(
        f"{BASE_URL}/api/place_order",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
        timeout=30,
    )
    
    if resp.status_code == 200:
        try:
            data = resp.json()
            if data.get("success") and data.get("order_id"):
                return int(data["order_id"])
        except Exception:
            pass
    
    return None