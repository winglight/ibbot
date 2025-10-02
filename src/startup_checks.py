"""Utilities for recording startup health checks.

This module provides a lightweight registry that startup hooks can use to
publish health check results. The registry is intentionally simple so that
tests may assert on the recorded results without needing to spin up the full
FastAPI application.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Mapping, Sequence
import os

__all__ = [
    "detect_open_sqlite_handles",
    "get_sqlite_handle_check",
    "get_startup_checks",
    "record_sqlite_handle_check",
    "reset_startup_checks",
]


_STARTUP_CHECKS: dict[str, dict[str, Any]] = {}
_LOCK = Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def reset_startup_checks() -> None:
    """Clear all recorded startup check results.

    Primarily used from unit tests to ensure isolation between runs.
    """

    with _LOCK:
        _STARTUP_CHECKS.clear()


def _record_startup_check(
    name: str,
    healthy: bool,
    *,
    detail: str | None = None,
    payload: Mapping[str, Any] | None = None,
) -> None:
    entry: dict[str, Any] = {
        "name": name,
        "healthy": bool(healthy),
        "checked_at": _now_iso(),
    }
    if detail:
        entry["detail"] = detail
    if payload:
        entry["payload"] = dict(payload)

    with _LOCK:
        _STARTUP_CHECKS[name] = entry


def record_sqlite_handle_check(handles: Sequence[str]) -> None:
    """Record the result of the SQLite file handle startup check."""

    unique_handles = sorted(dict.fromkeys(handles))
    healthy = not unique_handles
    detail = (
        "No sqlite file handles detected."
        if healthy
        else f"Detected sqlite file handles: {', '.join(unique_handles)}"
    )
    payload: dict[str, Any] | None = None
    if unique_handles:
        payload = {"handles": unique_handles}

    _record_startup_check(
        "sqlite_handles",
        healthy,
        detail=detail,
        payload=payload,
    )


def get_sqlite_handle_check() -> dict[str, Any] | None:
    """Return the last recorded SQLite handle check, if any."""

    with _LOCK:
        result = _STARTUP_CHECKS.get("sqlite_handles")
        if result is None:
            return None
        return dict(result)


def get_startup_checks() -> list[dict[str, Any]]:
    """Return all recorded startup check results."""

    with _LOCK:
        return [dict(value) for value in _STARTUP_CHECKS.values()]


def detect_open_sqlite_handles(fd_root: Path | None = None) -> list[str]:
    """Inspect the current process for open SQLite-related file descriptors."""

    root = fd_root or Path("/proc/self/fd")
    if not root.exists():  # pragma: no cover - non-Linux environments
        return []

    handles: list[str] = []
    for entry in root.iterdir():
        try:
            target = os.readlink(entry)
        except OSError:
            continue

        if not target:
            continue
        if target.startswith(("socket:", "pipe:", "anon_inode:")):
            continue

        normalised = target.split(" (deleted)")[0]
        if "sqlite" in normalised.lower():
            handles.append(normalised)

    return sorted(dict.fromkeys(handles))

