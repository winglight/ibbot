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
import shutil
import subprocess

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


def _env_flag(name: str) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return False
    return raw.strip().lower() not in {"", "0", "false", "no", "off", "disabled"}


def _detect_open_sqlite_handles_via_proc(root: Path) -> list[str]:
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

    return handles


def _detect_open_sqlite_handles_via_lsof() -> list[str]:
    """Use ``lsof`` to inspect open files for the current process."""

    lsof_path = shutil.which("lsof")
    if not lsof_path:
        return []

    try:
        result = subprocess.run(  # noqa: S603 - lsof path resolved via shutil.which
            [lsof_path, "-F", "n", "-p", str(os.getpid())],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:  # pragma: no cover - defensive guard for unexpected environments
        return []

    output = result.stdout or ""
    handles: list[str] = []
    for line in output.splitlines():
        if not line or not line.startswith("n"):
            continue
        target = line[1:]
        if "sqlite" in target.lower():
            handles.append(target)

    return handles


def detect_open_sqlite_handles(fd_root: Path | None = None) -> list[str]:
    """Inspect the current process for open SQLite-related file descriptors."""

    root = fd_root or Path("/proc/self/fd")
    handles: set[str] = set()

    if root.exists():
        handles.update(_detect_open_sqlite_handles_via_proc(root))
    else:
        handles.update(_detect_open_sqlite_handles_via_lsof())

    if not handles and _env_flag("SQLITE_HANDLE_CHECK_USE_LSOF"):
        handles.update(_detect_open_sqlite_handles_via_lsof())

    return sorted(handles)

