from __future__ import annotations

import asyncio
import sys
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
