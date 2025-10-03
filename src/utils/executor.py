"""Shared asynchronous execution utilities.

This module centralises the creation of thread pools used by the application for
running blocking database and CPU heavy workloads without stalling the main
asyncio event loop.  The executors are managed as a singleton so that all
modules share the same resources and we can coordinate a clean shutdown during
application exit.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, Callable, Optional


ExecutorFunc = Callable[..., Any]


class AsyncExecutor:
    """Container object holding dedicated thread pools for different workloads."""

    def __init__(self, db_max_workers: int, cpu_max_workers: int) -> None:
        if db_max_workers <= 0:
            raise ValueError("db_max_workers must be a positive integer")
        if cpu_max_workers <= 0:
            raise ValueError("cpu_max_workers must be a positive integer")

        self._db_executor = ThreadPoolExecutor(
            max_workers=db_max_workers,
            thread_name_prefix="db-worker",
        )
        self._cpu_executor = ThreadPoolExecutor(
            max_workers=cpu_max_workers,
            thread_name_prefix="cpu-worker",
        )
        self._shutdown = False
        self._lock = threading.Lock()

    @property
    def db_executor(self) -> ThreadPoolExecutor:
        return self._db_executor

    @property
    def cpu_executor(self) -> ThreadPoolExecutor:
        return self._cpu_executor

    def run_in_db(self, func: ExecutorFunc, *args: Any, **kwargs: Any):
        return self._submit(self._db_executor, func, *args, **kwargs)

    def run_in_cpu(self, func: ExecutorFunc, *args: Any, **kwargs: Any):
        return self._submit(self._cpu_executor, func, *args, **kwargs)

    def shutdown(self, wait: bool = False) -> None:
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
        self._db_executor.shutdown(wait=wait, cancel_futures=not wait)
        self._cpu_executor.shutdown(wait=wait, cancel_futures=not wait)

    def _submit(
        self,
        executor: ThreadPoolExecutor,
        func: ExecutorFunc,
        *args: Any,
        **kwargs: Any,
    ):
        if self._shutdown:
            raise RuntimeError("AsyncExecutor has been shut down")

        call = partial(func, *args, **kwargs)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running event loop: fall back to the synchronous submit API.
            return executor.submit(call)
        else:
            return loop.run_in_executor(executor, call)


_executor_lock = threading.Lock()
_executor_instance: Optional[AsyncExecutor] = None


def configure_async_executor(db_max_workers: int, cpu_max_workers: int) -> AsyncExecutor:
    """Configure the global :class:`AsyncExecutor` instance.

    The first call creates the executor. Subsequent calls simply return the
    already-configured instance to avoid spawning additional thread pools.
    """

    global _executor_instance

    with _executor_lock:
        if _executor_instance is None:
            _executor_instance = AsyncExecutor(
                db_max_workers=db_max_workers,
                cpu_max_workers=cpu_max_workers,
            )
        return _executor_instance


def get_async_executor() -> AsyncExecutor:
    if _executor_instance is None:
        raise RuntimeError("AsyncExecutor has not been configured yet")
    return _executor_instance


def run_in_db_executor(func: ExecutorFunc, *args: Any, **kwargs: Any):
    return get_async_executor().run_in_db(func, *args, **kwargs)


def run_in_cpu_executor(func: ExecutorFunc, *args: Any, **kwargs: Any):
    return get_async_executor().run_in_cpu(func, *args, **kwargs)


def shutdown(wait: bool = False) -> None:
    if _executor_instance is None:
        return
    _executor_instance.shutdown(wait=wait)
