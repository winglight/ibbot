"""Helpers for restarting the running process in-place."""
from __future__ import annotations

import os
import sys
import time
from typing import Dict, Iterable, Optional

from loguru import logger


def restart_program(
    *,
    reason: Optional[str] = None,
    delay_sec: float = 0.0,
    extra_args: Optional[Iterable[str]] = None,
    env_updates: Optional[Dict[str, str]] = None,
) -> None:
    """Restart the current Python interpreter in-place using ``os.execve``.

    Parameters
    ----------
    reason:
        Optional message describing why the restart is happening (emitted to logs
        and exported via ``LAST_RESTART_REASON``).
    delay_sec:
        Optional delay before restarting to avoid tight restart loops.
    extra_args:
        Additional command line arguments appended to ``sys.argv``.
    env_updates:
        Extra environment variables merged into the environment passed to the
        restarted process.
    """
    if reason:
        logger.critical(f"Restarting process due to: {reason}")
    else:
        logger.critical("Restarting process (no reason supplied)")

    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass

    if delay_sec > 0:
        try:
            time.sleep(delay_sec)
        except Exception:
            pass

    argv = [sys.executable] + sys.argv
    if extra_args:
        argv.extend(list(extra_args))

    new_env = os.environ.copy()
    if env_updates:
        new_env.update(env_updates)

    try:
        restart_count = int(new_env.get("RESTART_COUNT", "0")) + 1
    except Exception:
        restart_count = 1
    new_env["RESTART_COUNT"] = str(restart_count)

    if reason:
        new_env["LAST_RESTART_REASON"] = reason

    os.execve(sys.executable, argv, new_env)
