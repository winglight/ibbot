"""Compatibility helpers for dataclasses across Python versions."""

from __future__ import annotations

import dataclasses
import sys
from functools import wraps
from typing import Any, Callable


def enable_slots_compat() -> None:
    """Allow ``@dataclass(slots=True)`` on Python < 3.10 by stripping the flag."""

    if sys.version_info >= (3, 10):
        return

    original = dataclasses.dataclass

    @wraps(original)
    def _compat_dataclass(*args: Any, **kwargs: Any):
        kwargs.pop("slots", None)
        return original(*args, **kwargs)

    dataclasses.dataclass = _compat_dataclass  # type: ignore[assignment]
