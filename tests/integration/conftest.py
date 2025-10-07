"""Shared fixtures for integration tests that exercise the real FastAPI app."""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Iterator
from pathlib import Path
from typing import Dict

import pytest
from fastapi.testclient import TestClient

from tests.services.redis_utils import InMemoryRedis


ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin123"
VISITOR_USERNAME = "visitor"
VISITOR_PASSWORD = "visitor123"

IBKR_HOST = "integration-host"
IBKR_PORT = 4100
IBKR_CLIENT_ID = 17
IBKR_PAPER_TRADING = "true"


@pytest.fixture(scope="session", autouse=True)
def _configure_core_env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Ensure the core environment variables exist for the app bootstrap."""

    tmp_dir = tmp_path_factory.mktemp("integration-db")
    db_file = tmp_dir / "ibbot.db"

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("IBBOT_DB_URL", f"sqlite:///{db_file}")
    monkeypatch.setenv("DATA_LAYER_DB_URL", f"sqlite:///{db_file}")
    monkeypatch.setenv("AUTO_CONNECT", "false")
    monkeypatch.setenv("ADMIN_USERNAME", ADMIN_USERNAME)
    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("VISITOR_USERNAME", VISITOR_USERNAME)
    monkeypatch.setenv("VISITOR_PASSWORD", VISITOR_PASSWORD)
    monkeypatch.setenv("JWT_SECRET", "integration-test-secret")
    monkeypatch.setenv("IBKR_HOST", IBKR_HOST)
    monkeypatch.setenv("IBKR_PORT", str(IBKR_PORT))
    monkeypatch.setenv("IBKR_CLIENT_ID", str(IBKR_CLIENT_ID))
    monkeypatch.setenv("IBKR_PAPER_TRADING", IBKR_PAPER_TRADING)

    # ``src.routes.auth`` reads the credentials at import time.  Ensure it
    # observes the integration defaults even if the module was imported before
    # this fixture executed (for example by other tests collected earlier).
    auth_module = importlib.import_module("src.routes.auth")
    importlib.reload(auth_module)
    monkeypatch.setattr(auth_module, "ADMIN_USERNAME", ADMIN_USERNAME, raising=False)
    monkeypatch.setattr(auth_module, "ADMIN_PASSWORD", ADMIN_PASSWORD, raising=False)
    monkeypatch.setattr(auth_module, "VISITOR_USERNAME", VISITOR_USERNAME, raising=False)
    monkeypatch.setattr(auth_module, "VISITOR_PASSWORD", VISITOR_PASSWORD, raising=False)

    legacy_db = Path("data/trading_bot.db")
    if legacy_db.exists():
        legacy_db.unlink()

    yield

    monkeypatch.undo()
    if legacy_db.exists():
        legacy_db.unlink()


@pytest.fixture(scope="session")
def _fake_redis() -> Iterator[InMemoryRedis]:
    """Provide an in-memory Redis replacement for the application."""

    fake = InMemoryRedis()
    patch = pytest.MonkeyPatch()

    async def _get_client(*_args, **_kwargs) -> InMemoryRedis:
        return fake

    async def _ensure_client(redis: InMemoryRedis | None = None) -> InMemoryRedis:
        return redis or fake

    async def _close_global_client() -> None:
        return None

    patch.setattr("src.ibbot.redis.get_redis_client", _get_client)
    patch.setattr("src.ibbot.redis._ensure_client", _ensure_client)
    patch.setattr("src.ibbot.redis.close_global_client", _close_global_client)

    yield fake

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(fake.flushall())
    finally:
        loop.close()

    patch.undo()


@pytest.fixture(scope="session")
def app(_configure_core_env: None, _fake_redis: InMemoryRedis):  # noqa: PT004
    """Return the FastAPI app with integration fixtures wired in."""

    import main

    importlib.reload(main)
    return main.app


@pytest.fixture()
def client(app) -> Iterator[TestClient]:  # type: ignore[override]
    """Provide a synchronous HTTP client for exercising the API."""

    with TestClient(app) as http_client:
        yield http_client


def _login(client: TestClient, username: str, password: str) -> Dict[str, str]:
    response = client.post(
        "/api/login",
        json={"username": username, "password": password},
    )
    response.raise_for_status()
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def admin_headers(client: TestClient) -> Dict[str, str]:
    """Return authorization headers for the admin user."""

    return _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)


@pytest.fixture()
def visitor_headers(client: TestClient) -> Dict[str, str]:
    """Return authorization headers for the read-only visitor user."""

    return _login(client, VISITOR_USERNAME, VISITOR_PASSWORD)


@pytest.fixture(scope="session")
def expected_connection_config() -> Dict[str, int | str | bool]:
    """Return the expected connection configuration resolved from the environment."""

    return {
        "host": IBKR_HOST,
        "port": IBKR_PORT,
        "client_id": IBKR_CLIENT_ID,
        "paper_trading": True,
    }
