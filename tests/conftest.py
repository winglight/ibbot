from __future__ import annotations

import asyncio
import inspect
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from tests.services.redis_utils import InMemoryRedis


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(scope="session", autouse=True)
def _enforce_no_sqlite() -> None:
    """Ensure the legacy SQLite database is never created during tests."""

    db_path = Path("data/trading_bot.db")
    if db_path.exists():
        db_path.unlink()

    yield

    assert not db_path.exists(), "tests must not create data/trading_bot.db"


@pytest.fixture
def db_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    """Configure the async database URL for tests."""

    from src.ibbot.db import config as db_config
    from src.ibbot.db import session as db_session

    existing = os.getenv("IBBOT_DB_URL")
    if existing:
        db_config.get_database_url.cache_clear()
        db_session._ENGINE = None
        db_session._SESSION_FACTORY = None
        yield existing
        db_session._ENGINE = None
        db_session._SESSION_FACTORY = None
        return

    db_file = tmp_path / "ibbot.db"
    url = f"sqlite+aiosqlite:///{db_file}"
    monkeypatch.setenv("IBBOT_DB_URL", url)
    monkeypatch.setenv("DATA_LAYER_DB_URL", url)
    db_config.get_database_url.cache_clear()
    db_session._ENGINE = None
    db_session._SESSION_FACTORY = None
    yield url
    db_session._ENGINE = None
    db_session._SESSION_FACTORY = None


@pytest.fixture
def db_engine(db_url: str):
    """Initialise the metadata on the configured database."""

    from src.ibbot.db import schemas
    from src.ibbot.db import session as db_session

    engine = db_session.get_async_engine()

    async def _prepare() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(schemas.metadata.drop_all)
            await connection.run_sync(schemas.metadata.create_all)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_prepare())
    finally:
        loop.close()

    yield engine

    async def _dispose() -> None:
        await engine.dispose()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_dispose())
    finally:
        loop.close()
    db_session._ENGINE = None
    db_session._SESSION_FACTORY = None


@pytest.fixture
def db_session_factory(db_engine):
    """Return the async session factory bound to the test database."""

    from src.ibbot.db.session import get_session_factory

    return get_session_factory()


@pytest.fixture
def repository_factory(db_session_factory):
    """Provide an async context manager to construct repositories with fresh sessions."""

    @asynccontextmanager
    async def _factory(constructor):
        async with db_session_factory() as session:
            yield constructor(session)

    return _factory


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
