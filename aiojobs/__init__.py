"""Minimal in-repo aiojobs compatible scheduler.

This lightweight implementation mirrors the small subset of the
`aiojobs` API that the project relies on while avoiding an external
dependency.  It provides :func:`create_scheduler`, :class:`Scheduler`
and :class:`Job` objects with the same behaviour used by the queue
manager.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Set

__all__ = ["create_scheduler", "Scheduler", "Job"]


class Job:
    """Wrapper around an asyncio.Task with a cooperative close hook."""

    def __init__(self, task: asyncio.Task, finalizer: Callable[["Job"], None]):
        self._task = task
        self._finalizer = finalizer

    def done(self) -> bool:
        return self._task.done()

    async def wait(self) -> None:
        await asyncio.shield(self._task)

    async def close(self) -> None:
        if not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._finalizer(self)


class Scheduler:
    """Very small scheduler providing limited concurrency control."""

    def __init__(self, limit: int = 10):
        if limit <= 0:
            raise ValueError("Scheduler limit must be positive")
        self._limit = limit
        self._semaphore = asyncio.Semaphore(limit)
        self._jobs: Set[Job] = set()
        self._closing = False

    @property
    def limit(self) -> int:
        return self._limit

    def active_count(self) -> int:
        return len(self._jobs)

    async def spawn(self, coro: Awaitable[None]) -> Job:
        if self._closing:
            raise RuntimeError("Scheduler is closing")

        await self._semaphore.acquire()

        async def runner() -> None:
            try:
                await coro
            finally:
                self._semaphore.release()

        task = asyncio.create_task(runner())
        job = Job(task, self._finalize_job)
        self._jobs.add(job)
        task.add_done_callback(lambda _: self._finalize_job(job))
        return job

    def _finalize_job(self, job: Job) -> None:
        self._jobs.discard(job)

    async def close(self) -> None:
        self._closing = True
        jobs = list(self._jobs)
        for job in jobs:
            await job.close()
        self._jobs.clear()


async def create_scheduler(*, limit: int = 10) -> Scheduler:
    return Scheduler(limit=limit)
