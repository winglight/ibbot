"""Lightweight background worker helpers used by services."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Set


@dataclass(slots=True)
class WorkerTask:
    """Wrapper around ``asyncio.Task`` exposing convenience helpers."""

    task: asyncio.Task[Any]

    def cancel(self) -> None:
        if not self.task.done():
            self.task.cancel()

    def done(self) -> bool:
        return self.task.done()

    async def result(self) -> Any:
        return await self.task


class WorkerQueue:
    """Simple background worker queue compatible with FastAPI lifecycles."""

    def __init__(self, *, name: str | None = None) -> None:
        self._name = name or "fastapi-worker"
        self._tasks: Set[asyncio.Task[Any]] = set()
        self._lock = asyncio.Lock()

    async def spawn(self, coro: Awaitable[Any]) -> WorkerTask:
        """Schedule *coro* in the background and return a ``WorkerTask`` handle."""

        if not asyncio.iscoroutine(coro):
            raise TypeError("spawn() requires an awaitable coroutine")

        task = asyncio.create_task(self._wrap(coro))
        async with self._lock:
            self._tasks.add(task)
        task.add_done_callback(lambda finished: self._tasks.discard(finished))
        return WorkerTask(task)

    async def _wrap(self, coro: Awaitable[Any]) -> Any:
        try:
            return await coro
        finally:
            # Ensure task removal even when awaited concurrently.
            async with self._lock:
                current = asyncio.current_task()
                if current is not None:
                    self._tasks.discard(current)

    async def shutdown(self, *, cancel: bool = True) -> None:
        """Cancel and await all running tasks."""

        async with self._lock:
            tasks = list(self._tasks)
            self._tasks.clear()

        if cancel:
            for task in tasks:
                task.cancel()

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def drain(self) -> None:
        """Wait until all scheduled tasks complete without cancelling."""

        async with self._lock:
            tasks = list(self._tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


__all__ = ["WorkerQueue", "WorkerTask"]
