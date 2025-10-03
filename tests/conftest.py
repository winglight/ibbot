from __future__ import annotations

import asyncio
import inspect
import sqlite3
import sys
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from src.db import market_event_writer, order_repository, service_metric_writer, set_engine
from tests.services.redis_utils import InMemoryRedis

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


TEST_DB_SCHEMA = """
CREATE TABLE orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT UNIQUE NOT NULL,
    symbol TEXT,
    side TEXT,
    status TEXT,
    exchange TEXT,
    quantity REAL,
    price REAL,
    order_timestamp TEXT,
    raw_payload TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    filled_quantity REAL,
    remaining_quantity REAL,
    avg_fill_price REAL,
    limit_price REAL,
    stop_price REAL,
    commission REAL,
    realized_pnl REAL,
    unrealized_pnl REAL,
    trigger_price REAL,
    order_type TEXT,
    trigger TEXT,
    trigger_type TEXT,
    trigger_status TEXT,
    notes TEXT,
    account TEXT,
    source TEXT,
    strategy TEXT,
    sec_type TEXT
);
CREATE UNIQUE INDEX ix_orders_order_id ON orders(order_id);

CREATE TABLE market_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    topic TEXT NOT NULL,
    symbol TEXT,
    source TEXT,
    payload TEXT,
    meta TEXT,
    ts_ns INTEGER,
    recorded_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL
);

CREATE TABLE service_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL NOT NULL,
    status TEXT,
    host TEXT,
    details TEXT,
    recorded_at TEXT NOT NULL
);

CREATE TABLE market_prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    bid REAL,
    ask REAL,
    bid_size REAL,
    ask_size REAL,
    source TEXT,
    observed_at TEXT NOT NULL,
    extra TEXT
);
"""


def _initialise_sqlite_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(TEST_DB_SCHEMA)


@pytest.fixture()
def sqlite_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[sqlite3.Connection]:
    """Configure an in-memory SQLite database for DB-API repositories."""

    connection = sqlite3.connect(
        "file:ibbot-tests?mode=memory&cache=shared", uri=True, check_same_thread=False
    )
    connection.row_factory = sqlite3.Row
    _initialise_sqlite_schema(connection)

    set_engine(connection, close=False)
    monkeypatch.setenv("IBBOT_DB_URL", "sqlite:///:memory:")
    monkeypatch.setenv("DATA_LAYER_DB_URL", "sqlite:///:memory:")

    try:
        yield connection
    finally:
        set_engine(None)
        connection.close()
        monkeypatch.delenv("IBBOT_DB_URL", raising=False)
        monkeypatch.delenv("DATA_LAYER_DB_URL", raising=False)


def _async_factory(factory: Callable[[], Any]) -> Callable[[], Awaitable[Any] | Any]:
    async def _wrapper() -> Any:
        return factory()

    return _wrapper


@pytest.fixture()
def order_repository_factory(sqlite_db: sqlite3.Connection) -> Callable[[], Awaitable[Any] | Any]:
    """Return an async-compatible factory for the order repository."""

    return _async_factory(order_repository)


@pytest.fixture()
def market_event_repository_factory(
    sqlite_db: sqlite3.Connection,
) -> Callable[[], Awaitable[Any] | Any]:
    """Return an async-compatible factory for the market event repository."""

    return _async_factory(market_event_writer)


@pytest.fixture()
def service_metric_repository_factory(
    sqlite_db: sqlite3.Connection,
) -> Callable[[], Awaitable[Any] | Any]:
    """Return an async-compatible factory for the service metric repository."""

    return _async_factory(service_metric_writer)


@pytest.fixture(scope="session")
def fake_redis_client() -> InMemoryRedis:
    fake = InMemoryRedis()
    patch = pytest.MonkeyPatch()

    async def _get_client(*_args, **_kwargs) -> InMemoryRedis:
        return fake

    async def _ensure_client(redis: InMemoryRedis | None = None) -> InMemoryRedis:
        return redis or fake

    async def _close_global_client() -> None:
        return None

    patch.setenv("IBBOT_REDIS_URL", "redis://localhost/0")
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


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "asyncio: execute test within an event loop")
