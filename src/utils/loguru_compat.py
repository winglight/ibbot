"""Compatibility helpers to bridge standard logging style with Loguru."""
from __future__ import annotations

from functools import wraps
from typing import Callable, Iterable, Tuple

from loguru import logger

_PERCENT_STYLE_ENABLED_ATTR = "_ibbot_percent_style_enabled"
_PERCENT_PLACEHOLDERS = ("%s", "%d", "%i", "%r", "%f", "%g", "%x", "%o", "%%")


def _needs_percent_format(message: str, args: tuple[object, ...]) -> bool:
    if not args:
        return False
    return any(token in message for token in _PERCENT_PLACEHOLDERS)


def _format_percent_style(message: str, args: tuple[object, ...]) -> Tuple[str, tuple[object, ...]]:
    if not isinstance(message, str) or not _needs_percent_format(message, args):
        return message, args
    try:
        formatted = message % args
    except Exception:
        return message, args
    return formatted, ()


def enable_percent_style_logging() -> None:
    """Allow Loguru to accept ``%``-style formatting arguments."""

    if getattr(logger, _PERCENT_STYLE_ENABLED_ATTR, False):
        return

    methods: Iterable[str] = (
        "trace",
        "debug",
        "info",
        "success",
        "warning",
        "error",
        "exception",
        "critical",
        "log",
    )

    for name in methods:
        original = getattr(logger, name)

        def make_wrapper(method: Callable[..., object]) -> Callable[..., object]:
            @wraps(method)
            def wrapper(message: object, *args: object, **kwargs: object) -> object:
                if isinstance(message, str):
                    formatted_message, remaining = _format_percent_style(
                        message,
                        args,  # type: ignore[arg-type]
                    )
                    if remaining is not args:
                        return method(formatted_message, *remaining, **kwargs)
                return method(message, *args, **kwargs)

            return wrapper

        setattr(logger, name, make_wrapper(original))

    setattr(logger, _PERCENT_STYLE_ENABLED_ATTR, True)

