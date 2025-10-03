"""Database configuration helpers for the ibbot data access layer.

This module resolves the async SQLAlchemy URL used by the services.  The
connection string is expected to use the ``mysql+asyncmy`` dialect and is read
from ``IBBOT_DB_URL``.  For backward compatibility with the historical data
layer the resolver falls back to ``DATA_LAYER_DB_URL`` when the primary
variable is not set.

Example
-------
>>> from src.ibbot.db.config import get_database_url
>>> get_database_url()
"mysql+asyncmy://user:pass@host:3306/ibbot"
"""

from __future__ import annotations

from functools import lru_cache
import os
from typing import Mapping

__all__ = ["get_database_url", "resolve_database_url", "DatabaseConfigError"]

SUPPORTED_DRIVER = "mysql+asyncmy"
PRIMARY_ENV_VAR = "IBBOT_DB_URL"
FALLBACK_ENV_VAR = "DATA_LAYER_DB_URL"


class DatabaseConfigError(RuntimeError):
    """Raised when the database configuration is invalid or missing."""


def _coerce_url(value: str):
    """Parse ``value`` into a SQLAlchemy :class:`~sqlalchemy.engine.URL`.

    Parameters
    ----------
    value:
        Raw database URL string.

    Raises
    ------
    DatabaseConfigError
        If the URL cannot be parsed or uses an unsupported driver.
    """

    try:
        from sqlalchemy.engine.url import make_url
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
        raise DatabaseConfigError("SQLAlchemy is required to parse database URLs") from exc

    try:
        url = make_url(value)
    except Exception as exc:  # pragma: no cover - defensive
        raise DatabaseConfigError("Invalid database URL") from exc

    if url.drivername != SUPPORTED_DRIVER:
        raise DatabaseConfigError(
            f"Unsupported database dialect '{url.drivername}'. Expected {SUPPORTED_DRIVER!r}."
        )
    return url


def resolve_database_url(env: Mapping[str, str] | None = None) -> str:
    """Return the configured database URL.

    Parameters
    ----------
    env:
        Optional mapping used for environment lookups.  When omitted the
        function reads from :data:`os.environ`.

    Returns
    -------
    str
        Normalised SQLAlchemy URL string.

    Raises
    ------
    DatabaseConfigError
        If neither environment variable is defined or the URL is invalid.
    """

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
