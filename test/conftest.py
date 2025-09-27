import importlib
import os
import shutil
import asyncio
import inspect
from pathlib import Path
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

DATABASE_BASENAME = "trading_bot.db"


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


@pytest.fixture(scope="session", autouse=True)
def isolate_database(tmp_path_factory):
    """备份并重置 data/trading_bot.db，测试结束后恢复。"""

    data_dir = Path("data")
    data_dir.mkdir(exist_ok=True)
    backup_dir = tmp_path_factory.mktemp("db-backup")
    backups: list[tuple[Path, Path]] = []

    def _copy_if_exists(filename: str) -> None:
        src = data_dir / filename
        if src.exists():
            dst = backup_dir / filename
            shutil.copy(src, dst)
            backups.append((src, dst))
            src.unlink()

    for suffix in ("", "-wal", "-shm"):
        _copy_if_exists(f"{DATABASE_BASENAME}{suffix}")

    yield

    # 清理测试产生的数据库文件
    for suffix in ("", "-wal", "-shm"):
        test_file = data_dir / f"{DATABASE_BASENAME}{suffix}"
        if test_file.exists():
            test_file.unlink()

    # 还原原始数据库
    for target, backup in backups:
        shutil.copy(backup, target)


@pytest.fixture(scope="session")
def app(isolate_database):  # type: ignore[unused-ignore]
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
