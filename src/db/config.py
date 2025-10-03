"""Database configuration helpers for :mod:`src.db`."""

from __future__ import annotations

from functools import lru_cache
import os
from typing import Mapping

from sqlalchemy.engine import URL
from sqlalchemy.engine.url import make_url

__all__ = ["get_database_url", "resolve_database_url", "DatabaseConfigError"]

SUPPORTED_DRIVERS = {"mysql+asyncmy", "sqlite+aiosqlite"}
PRIMARY_ENV_VAR = "IBBOT_DB_URL"
FALLBACK_ENV_VAR = "DATA_LAYER_DB_URL"


class DatabaseConfigError(RuntimeError):
    """Raised when the database configuration is invalid or missing."""


def _coerce_url(value: str) -> URL:
    """Parse *value* into a SQLAlchemy :class:`~sqlalchemy.engine.URL`."""

    try:
        url = make_url(value)
    except Exception as exc:  # pragma: no cover - defensive
        raise DatabaseConfigError("Invalid database URL") from exc

    if url.drivername not in SUPPORTED_DRIVERS:
        supported = ", ".join(sorted(SUPPORTED_DRIVERS))
        raise DatabaseConfigError(
            f"Unsupported database dialect '{url.drivername}'. Expected one of {supported}."
        )
    return url


def resolve_database_url(env: Mapping[str, str] | None = None) -> str:
    """Return the configured database URL."""

    source = env or os.environ
    raw_url = source.get(PRIMARY_ENV_VAR) or source.get(FALLBACK_ENV_VAR)
    if not raw_url:
        raise DatabaseConfigError(
            "Database URL was not provided. Set IBBOT_DB_URL or DATA_LAYER_DB_URL."
        )

    url = _coerce_url(raw_url.strip())
    return str(url)


@lru_cache(maxsize=1)
def get_database_url() -> str:
    """Return the cached database URL for the current process."""

    return resolve_database_url()
