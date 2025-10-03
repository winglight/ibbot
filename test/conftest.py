import asyncio
import importlib
import inspect
import os
from pathlib import Path
from typing import Dict

import pytest
from fastapi.testclient import TestClient

from tests.services.redis_utils import InMemoryRedis


# 固定测试环境变量，避免依赖外部 .env
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "admin123")
os.environ.setdefault("VISITOR_USERNAME", "visitor")
os.environ.setdefault("VISITOR_PASSWORD", "visitor123")
os.environ.setdefault("JWT_SECRET", "unit-test-secret")
os.environ.setdefault("AUTO_CONNECT", "false")


@pytest.fixture(scope="session", autouse=True)
def configure_db_env(tmp_path_factory: pytest.TempPathFactory):
    """Ensure application tests have a database URL configured."""

    existing = os.getenv("IBBOT_DB_URL")
    if existing:
        yield existing
        return

    db_file = tmp_path_factory.mktemp("functional-db") / "ibbot.db"
    url = f"sqlite:///{db_file}"
    os.environ.setdefault("IBBOT_DB_URL", url)
    os.environ.setdefault("DATA_LAYER_DB_URL", url)
    yield url


@pytest.fixture(scope="session", autouse=True)
def _enforce_no_sqlite() -> None:
    """Ensure the legacy SQLite database is never created during tests."""

    db_path = Path("data/trading_bot.db")
    if db_path.exists():
        db_path.unlink()

    yield

    assert not db_path.exists(), "tests must not create data/trading_bot.db"


@pytest.fixture(scope="session")
def fake_redis_client() -> InMemoryRedis:
    """Provide a shared in-memory Redis replacement for the test session."""

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


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "asyncio: execute test within an event loop")


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> bool:
    test_function = pyfuncitem.obj
    if inspect.iscoroutinefunction(test_function):
        kwargs = {name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames}
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(test_function(**kwargs))
        finally:
            loop.close()
        return True
    return False


@pytest.fixture(scope="session")
def app(fake_redis_client: InMemoryRedis):
    import main

    importlib.reload(main)
    return main.app


@pytest.fixture(scope="session")
def client(app, fake_redis_client: InMemoryRedis):
    with TestClient(app) as test_client:
        yield test_client


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
    return _login(
        client,
        os.environ["ADMIN_USERNAME"],
        os.environ["ADMIN_PASSWORD"],
    )


@pytest.fixture()
def visitor_headers(client: TestClient) -> Dict[str, str]:
    return _login(
        client,
        os.environ["VISITOR_USERNAME"],
        os.environ["VISITOR_PASSWORD"],
    )
