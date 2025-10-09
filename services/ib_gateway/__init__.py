"""IB gateway service exposing a shared Interactive Brokers client."""

from __future__ import annotations

from typing import Any

__all__ = ["run_service", "main"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from . import main as _main

        return getattr(_main, name)
    raise AttributeError(name)
