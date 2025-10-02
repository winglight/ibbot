import importlib
import os
import asyncio
import inspect
from typing import Dict

import pytest
from fastapi.testclient import TestClient

# 固定测试环境变量，避免依赖外部 .env
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "admin123")
os.environ.setdefault("VISITOR_USERNAME", "visitor")
os.environ.setdefault("VISITOR_PASSWORD", "visitor123")
os.environ.setdefault("JWT_SECRET", "unit-test-secret")
os.environ.setdefault("AUTO_CONNECT", "false")

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
def app():
    import main

    importlib.reload(main)
    return main.app


@pytest.fixture(scope="session")
def client(app):
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
