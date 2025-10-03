"""Session factories and helpers for MariaDB access."""

from __future__ import annotations

from contextlib import asynccontextmanager
import threading
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from .config import get_database_url

__all__ = [
    "get_async_engine",
    "get_session_factory",
    "get_async_session",
    "session_scope",
]

_ENGINE_LOCK = threading.Lock()
_ENGINE: AsyncEngine | None = None
_SESSION_FACTORY: async_sessionmaker[AsyncSession] | None = None


def _create_engine() -> AsyncEngine:
    return create_async_engine(
        get_database_url(),
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args={"charset": "utf8mb4"},
    )


def get_async_engine() -> AsyncEngine:
    """Return a lazily constructed :class:`~sqlalchemy.ext.asyncio.AsyncEngine`."""

    global _ENGINE
    if _ENGINE is None:
        with _ENGINE_LOCK:
            if _ENGINE is None:
                _ENGINE = _create_engine()
    return _ENGINE


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return a cached :class:`~sqlalchemy.ext.asyncio.async_sessionmaker`."""

    global _SESSION_FACTORY
    if _SESSION_FACTORY is None:
        with _ENGINE_LOCK:
            if _SESSION_FACTORY is None:
                _SESSION_FACTORY = async_sessionmaker(
                    bind=get_async_engine(),
                    expire_on_commit=False,
                    class_=AsyncSession,
                )
    return _SESSION_FACTORY


def get_async_session() -> AsyncSession:
    """Convenience helper returning a new :class:`AsyncSession`."""

    return get_session_factory()()


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Provide a transactional scope around a series of operations."""

    session = get_async_session()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
