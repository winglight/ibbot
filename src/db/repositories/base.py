"""Common helpers for DB-API based repositories."""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Iterable

from src.db.mariadb import connect

__all__ = [
    "RepositoryError",
    "RepositoryValidationError",
    "json_dumps",
    "use_connection",
    "is_sqlite",
    "execute_write",
    "executemany_write",
]


class RepositoryError(RuntimeError):
    """Raised when a repository operation fails."""


class RepositoryValidationError(RepositoryError, ValueError):
    """Raised when a repository encounters invalid input."""


def json_dumps(value: Any) -> str:
    """Serialize *value* to JSON preserving non-ASCII characters."""

    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


@contextmanager
def use_connection() -> Any:
    """Yield a DB-API connection from :func:`src.db.mariadb.connect`."""

    with connect() as connection:
        yield connection


def is_sqlite(connection: Any) -> bool:
    """Return ``True`` if *connection* is backed by sqlite3."""

    module = getattr(connection.__class__, "__module__", "")
    return module.startswith("sqlite3")


def execute_write(connection: Any, sql: str, params: Any) -> None:
    """Execute a single write statement with error handling."""

    cursor = connection.cursor()
    try:
        cursor.execute(sql, params)
    except Exception:
        try:
            connection.rollback()
        except Exception:  # pragma: no cover - defensive
            pass
        raise
    else:
        try:
            connection.commit()
        except Exception:
            try:
                connection.rollback()
            except Exception:  # pragma: no cover - defensive
                pass
            raise
    finally:
        try:
            cursor.close()
        except Exception:  # pragma: no cover - defensive
            pass


def executemany_write(connection: Any, sql: str, params_seq: Iterable[Any]) -> None:
    """Execute ``executemany`` with commit/rollback semantics."""

    cursor = connection.cursor()
    try:
        cursor.executemany(sql, list(params_seq))
    except Exception:
        try:
            connection.rollback()
        except Exception:  # pragma: no cover - defensive
            pass
        raise
    else:
        try:
            connection.commit()
        except Exception:
            try:
                connection.rollback()
            except Exception:  # pragma: no cover - defensive
                pass
            raise
    finally:
        try:
            cursor.close()
        except Exception:  # pragma: no cover - defensive
            pass
