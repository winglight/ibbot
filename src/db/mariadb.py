"""Utilities for interacting with the MariaDB metrics store using DB-API."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import parse_qs, urlparse

from .config import DatabaseConfigError as _DatabaseConfigError
from .config import get_database_url as _get_configured_url

__all__ = [
    "MariaDBConfigurationError",
    "connect",
    "get_database_url",
    "get_service_metrics_columns",
    "run_sql_query",
    "set_engine",
]


class MariaDBConfigurationError(RuntimeError):
    """Raised when the MariaDB connection details are missing or invalid."""


_connection_factory: Callable[[], Any] | None = None
_should_close_connection: bool = True


def _connect_sqlite(url: str) -> Any:
    if url.startswith("sqlite+aiosqlite://"):
        url = "sqlite://" + url[len("sqlite+aiosqlite://") :]
    elif url.startswith("sqlite+aiosqlite:"):
        url = "sqlite:" + url[len("sqlite+aiosqlite:") :]

    path = url.replace("sqlite:///", "", 1) if url.startswith("sqlite:///") else url
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def _connect_mysql(url: str) -> Any:
    try:
        import pymysql  # type: ignore
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise MariaDBConfigurationError(
            "pymysql is required to connect to MariaDB; install it and try again"
        ) from exc

    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme == "mysql+asyncmy":
        scheme = "mysql"

    if scheme not in {"mysql", "mariadb", "mysql+pymysql"}:
        raise MariaDBConfigurationError(f"Unsupported database URL scheme: {parsed.scheme}")

    query_params = parse_qs(parsed.query)
    charset = query_params.get("charset", ["utf8mb4"])[0]
    database = parsed.path.lstrip("/") or None

    host = parsed.hostname or "localhost"
    port = parsed.port or 3306

    try:
        return pymysql.connect(  # pragma: no cover - depends on optional driver
            host=host,
            port=port,
            user=parsed.username or "",
            password=parsed.password or "",
            database=database,
            charset=charset,
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
        )
    except pymysql.err.MySQLError as exc:  # pragma: no cover - connection failures depend on env
        message = exc.args[1] if len(exc.args) > 1 else str(exc)
        raise MariaDBConfigurationError(
            f"Unable to connect to MariaDB at {host}:{port}: {message}"
        ) from exc


def set_engine(engine: Any | Callable[[], Any] | None, *, close: bool | None = None) -> None:
    """Override the global connection factory, primarily for tests."""

    global _connection_factory, _should_close_connection
    if engine is None:
        _connection_factory = None
        _should_close_connection = True
        return

    if callable(engine) and not hasattr(engine, "cursor"):
        _connection_factory = engine
        _should_close_connection = True if close is None else close
    else:
        _connection_factory = (lambda: engine)
        _should_close_connection = False if close is None else close


def _create_connection() -> tuple[Any, bool]:
    if _connection_factory is not None:
        return _connection_factory(), _should_close_connection

    try:
        url = _get_configured_url()
    except _DatabaseConfigError as exc:  # pragma: no cover - validated by config tests
        raise MariaDBConfigurationError(str(exc)) from exc
    if url.startswith("sqlite://"):
        connection = _connect_sqlite(url)
        return connection, True
    return _connect_mysql(url), True


@contextmanager
def connect() -> Iterator[Any]:
    connection, should_close = _create_connection()
    try:
        yield connection
    finally:
        if should_close:
            try:
                connection.close()
            except Exception:  # pragma: no cover - defensive cleanup
                pass


def get_database_url() -> str:
    """Return the configured database URL."""

    try:
        return _get_configured_url()
    except _DatabaseConfigError as exc:  # pragma: no cover - validated by config tests
        raise MariaDBConfigurationError(str(exc)) from exc


def _extract_mapping(row: Any, index: int, key: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(key)
    return row[index]


def get_service_metrics_columns() -> list[str]:
    """Return the available columns from the ``service_metrics`` table."""

    with connect() as connection:
        cursor = connection.cursor()
        try:
            try:
                cursor.execute("PRAGMA table_info('service_metrics')")
                rows = cursor.fetchall()
                if rows:
                    return [
                        _extract_mapping(row, 1, "name")
                        for row in rows
                        if _extract_mapping(row, 1, "name")
                    ]
            finally:
                cursor.close()
        except Exception:
            pass

    with connect() as connection:
        cursor = connection.cursor()
        try:
            cursor.execute("SHOW COLUMNS FROM service_metrics")
            rows = cursor.fetchall()
            if rows:
                return [
                    _extract_mapping(row, 0, "Field")
                    for row in rows
                    if _extract_mapping(row, 0, "Field")
                ]
        except Exception as exc:
            raise MariaDBConfigurationError(
                "Unable to introspect columns for service_metrics"
            ) from exc
        finally:
            cursor.close()

    raise MariaDBConfigurationError("Table `service_metrics` does not exist")


def run_sql_query(query: str, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Execute *query* and return the rows as dictionaries."""

    with connect() as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(query, params or {})
            description = getattr(cursor, "description", None) or []
            columns = [col[0] for col in description]
            rows = cursor.fetchall() or []

            results: list[dict[str, Any]] = []
            for row in rows:
                if isinstance(row, Mapping):
                    results.append(dict(row))
                else:
                    record = {columns[idx]: row[idx] for idx in range(len(columns))}
                    results.append(record)
            return results
        finally:
            cursor.close()
