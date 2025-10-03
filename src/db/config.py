"""Database configuration helpers for :mod:`src.db`."""

from __future__ import annotations

from functools import lru_cache
import os
from typing import Mapping
from urllib.parse import urlparse

__all__ = ["get_database_url", "resolve_database_url", "DatabaseConfigError"]

SUPPORTED_DRIVERS = {
    "mariadb",
    "mysql",
    "mysql+asyncmy",
    "mysql+pymysql",
    "sqlite",
    "sqlite+aiosqlite",
}
PRIMARY_ENV_VAR = "IBBOT_DB_URL"
FALLBACK_ENV_VAR = "DATA_LAYER_DB_URL"


class DatabaseConfigError(RuntimeError):
    """Raised when the database configuration is invalid or missing."""


def _validate_url(value: str) -> None:
    try:
        parsed = urlparse(value)
    except Exception as exc:  # pragma: no cover - defensive
        raise DatabaseConfigError("Invalid database URL") from exc

    scheme = (parsed.scheme or "").lower()
    if not scheme:
        raise DatabaseConfigError("Database URL is missing a scheme")

    if scheme not in SUPPORTED_DRIVERS:
        supported = ", ".join(sorted(SUPPORTED_DRIVERS))
        raise DatabaseConfigError(
            f"Unsupported database dialect '{scheme}'. Expected one of {supported}."
        )

    if scheme.startswith("sqlite"):
        if parsed.path in {"", "/"} and not parsed.netloc:
            raise DatabaseConfigError("SQLite database path was not provided")
    else:
        if not parsed.hostname:
            raise DatabaseConfigError("Database host was not provided")


def resolve_database_url(env: Mapping[str, str] | None = None) -> str:
    """Return the configured database URL."""

    source = env or os.environ
    raw_url = source.get(PRIMARY_ENV_VAR) or source.get(FALLBACK_ENV_VAR)
    if not raw_url:
        raise DatabaseConfigError(
            "Database URL was not provided. Set IBBOT_DB_URL or DATA_LAYER_DB_URL."
        )

    candidate = raw_url.strip()
    _validate_url(candidate)
    return candidate


@lru_cache(maxsize=1)
def get_database_url() -> str:
    """Return the cached database URL for the current process."""

    return resolve_database_url()
