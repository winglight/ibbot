"""Async helpers for running blocking DB-API operations."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, TypeVar

__all__ = ["run_in_thread"]

T = TypeVar("T")


async def run_in_thread(func: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Execute *func* in a thread executor and return the result."""

    try:
        return await asyncio.to_thread(func, *args, **kwargs)
    except AttributeError:  # pragma: no cover - Python < 3.9 fallback
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: func(*args, **kwargs))
