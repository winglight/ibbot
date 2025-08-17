import os
import pytest
import requests
from dotenv import load_dotenv
from typing import Optional

# Ensure environment variables from .env are loaded for tests
load_dotenv()

BASE_URL = os.getenv("TEST_BASE_URL", "http://localhost:8000")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change_me")
DEFAULT_SYMBOL = os.getenv("TEST_SYMBOL", "AAPL")


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