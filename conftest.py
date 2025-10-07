"""Project-level pytest configuration."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session", autouse=True)
def _shutdown_depth_subscriber() -> None:
    """Ensure the global ``DepthSubscriber`` thread pool shuts down for pytest."""

    # The production ``src.dom_dependencies`` module instantiates a global
    # :class:`DepthSubscriber` when imported.  Its ``ThreadPoolExecutor`` uses
    # non-daemon worker threads, which keep the interpreter alive after the test
    # session completes.  In the real application ``atexit`` handles cleanup, but
    # pytest does not exit when those threads are still running.  Explicitly
    # shutting down the executor allows ``pytest`` to terminate cleanly without
    # relying on a keyboard interrupt.
    from src.dom_dependencies import depth_subscriber

    yield

    executor = getattr(depth_subscriber, "_callback_executor", None)
    try:
        shutdown = getattr(depth_subscriber, "_shutdown_executor", None)
        if callable(shutdown):
            shutdown()
    finally:
        if executor is not None:
            try:
                executor.shutdown(wait=True)
            except Exception:
                pass
