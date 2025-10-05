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
    command_id TEXT,
    order_id TEXT,
    client_order_id TEXT,
    ib_order_id TEXT,
    symbol TEXT NOT NULL,
    action TEXT,
    side TEXT,
    quantity REAL,
    order_type TEXT,
    price REAL,
    limit_price REAL,
    stop_price REAL,
    status TEXT DEFAULT 'Submitted',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    executed_at TEXT,
    order_timestamp TEXT,
    fill_price REAL,
    filled_quantity REAL,
    remaining_quantity REAL,
    avg_fill_price REAL,
    order_source TEXT,
    strategy TEXT,
    strategy_name TEXT,
    rule_id TEXT,
    parent_order_id TEXT,
    exchange TEXT DEFAULT 'SMART',
    sec_type TEXT DEFAULT 'STK',
    notes TEXT,
    commission REAL,
    pnl REAL,
    realized_pnl REAL,
    unrealized_pnl REAL,
    trigger TEXT,
    trigger_type TEXT,
    trigger_status TEXT,
    trigger_price REAL,
    account TEXT,
    source TEXT,
    raw_payload TEXT,
    is_deleted INTEGER DEFAULT 0
);
CREATE UNIQUE INDEX ix_orders_command_id ON orders(command_id);
CREATE UNIQUE INDEX ix_orders_client_order_id ON orders(client_order_id);
CREATE UNIQUE INDEX ix_orders_ib_order_id ON orders(ib_order_id);
CREATE INDEX ix_orders_symbol ON orders(symbol);
CREATE INDEX ix_orders_status_created ON orders(status, created_at);
CREATE INDEX ix_orders_strategy ON orders(strategy);
CREATE INDEX ix_orders_strategy_name ON orders(strategy_name);
CREATE INDEX ix_orders_parent ON orders(parent_order_id);
CREATE INDEX ix_orders_created ON orders(created_at);

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

CREATE TABLE risk_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT UNIQUE NOT NULL,
    stop_loss_price REAL,
    take_profit_price REAL,
    max_loss_percent REAL,
    enabled INTEGER DEFAULT 1,
    max_time_span INTEGER,
    timestamp TEXT NOT NULL,
    auto_trailing INTEGER DEFAULT 0,
    atr_params TEXT,
    rule_type TEXT DEFAULT 'fixed',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE strategies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    strategy_type TEXT NOT NULL,
    enabled INTEGER DEFAULT 1,
    api_endpoint TEXT,
    file_path TEXT,
    description TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE historical_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    time TEXT,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume INTEGER NOT NULL,
    interval TEXT NOT NULL,
    adj_close REAL
);

CREATE TABLE config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    config_key TEXT UNIQUE NOT NULL,
    config_value TEXT NOT NULL,
    description TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE sizing_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    suggested_quantity REAL NOT NULL,
    current_price REAL,
    account_value REAL,
    position_size REAL,
    risk_percent REAL,
    sizing_method TEXT NOT NULL,
    calculation_details TEXT,
    g_rules_checked TEXT,
    timestamp TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE notification_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    event_type TEXT NOT NULL,
    message TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    retry_count INTEGER DEFAULT 0,
    error_message TEXT,
    sent_at TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
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
