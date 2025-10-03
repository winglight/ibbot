"""Utility helpers for shared asynchronous executors.

This module exposes a process-wide singleton that provides a dedicated
ThreadPoolExecutor for blocking database operations.  SQLite allows
cross-thread access when ``check_same_thread=False`` is set, but we still
serialize calls via the executor to avoid starving the main event loop.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from typing import Optional

__all__ = ["AsyncExecutor", "get_async_executor", "set_async_executor"]


class AsyncExecutor:
    """Container object that owns thread pool executors for blocking tasks."""

    def __init__(self, *, db_executor: Optional[ThreadPoolExecutor] = None) -> None:
        max_workers = int(os.getenv("DB_EXECUTOR_WORKERS", "1"))
        self._db_executor = db_executor or ThreadPoolExecutor(max_workers=max_workers)

    @property
    def db_executor(self) -> ThreadPoolExecutor:
        return self._db_executor

    def shutdown(self, wait: bool = False) -> None:
        self._db_executor.shutdown(wait=wait)


_async_executor: Optional[AsyncExecutor] = None
_async_executor_lock: Lock = Lock()


def get_async_executor() -> AsyncExecutor:
    """Return the process-wide asynchronous executor singleton."""

    global _async_executor
    if _async_executor is None:
        with _async_executor_lock:
            if _async_executor is None:
                _async_executor = AsyncExecutor()
    return _async_executor


def set_async_executor(executor: AsyncExecutor) -> None:
    """Override the global asynchronous executor (useful for tests)."""

    global _async_executor
    with _async_executor_lock:
        _async_executor = executor
