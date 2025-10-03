"""Utility helpers used across the project.

This module centralises timezone conversions so that all timestamps stored in
or returned by the application consistently use the Asia/Shanghai (+08:00)
timezone.  IBKR data continues to arrive in US/Eastern and therefore requires
conversion before being surfaced to clients.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime
from typing import Any, Optional, Tuple

from .executor import (
    AsyncExecutor,
    configure_async_executor,
    get_async_executor,
    run_in_cpu_executor,
    run_in_db_executor,
    shutdown as shutdown_async_executor,
)

import pandas as pd
from zoneinfo import ZoneInfo

try:  # Optional dependency for accurate US market calendars
    import pandas_market_calendars as mcal
except ImportError:  # pragma: no cover - fallback when dependency missing
    mcal = None


# Timezone helpers -----------------------------------------------------------
#
# IBKR timestamps are provided in US/Eastern time (America/New_York).  When
# converting these to the server's local timezone we need to be mindful of
# daylight saving time.  ``LOCAL_TZ`` is fixed to Asia/Shanghai so that the
# backend behaves consistently regardless of the host machine's timezone.

LOCAL_TZ = ZoneInfo("Asia/Shanghai")
NY_TZ = ZoneInfo("America/New_York")

_NYSE_CALENDAR = None


def now_local() -> datetime:
    """Return the current time in the configured local timezone."""
    return datetime.now(LOCAL_TZ)


def ensure_local_timezone(dt: Optional[datetime]) -> Optional[datetime]:
    """Attach/convert a datetime to the application's local timezone."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(LOCAL_TZ)


def parse_local_datetime(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-formatted string into a localised datetime.

    Strings with a trailing ``Z`` are treated as UTC and converted to the
    local timezone.  Naive strings (without timezone information) are assumed
    to already represent local time and thus receive the local timezone.
    """
    if not value:
        return None

    text = value.strip()
    if not text:
        return None

    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    dt = datetime.fromisoformat(text)
    return ensure_local_timezone(dt)


def to_local_iso(dt: Optional[datetime]) -> Optional[str]:
    """Serialise a datetime to ISO format with the local timezone attached."""
    if dt is None:
        return None
    return ensure_local_timezone(dt).isoformat()


def to_local_time(value: Any) -> str:
    """Convert IBKR timestamps (US/Eastern) to local timezone ISO strings."""
    try:
        if hasattr(value, "to_pydatetime"):
            value = value.to_pydatetime()
        if isinstance(value, datetime):
            if value.tzinfo is None:
                dt = pd.Timestamp(value, tz=NY_TZ).to_pydatetime()
            else:
                dt = value.astimezone(NY_TZ)
        else:
            dt = pd.Timestamp(value, tz=NY_TZ).to_pydatetime()
        return ensure_local_timezone(dt).isoformat()
    except Exception:
        return str(value)


# Market session helpers ----------------------------------------------------

def _ensure_nyse_calendar():
    """Return a cached pandas-market-calendars instance for XNYS."""

    global _NYSE_CALENDAR
    if _NYSE_CALENDAR is not None or mcal is None:
        return _NYSE_CALENDAR

    try:
        _NYSE_CALENDAR = mcal.get_calendar("XNYS")
    except Exception:  # pragma: no cover - defensive guard for missing data files
        _NYSE_CALENDAR = None
    return _NYSE_CALENDAR


def get_us_market_session(dt: Optional[datetime] = None) -> Tuple[bool, str]:
    """Return whether US equities market is open along with a human reason.

    Parameters
    ----------
    dt:
        Datetime to evaluate.  Defaults to ``now`` in New York time.

    Returns
    -------
    Tuple[bool, str]
        ``(is_open, reason)`` where ``reason`` explains why the market is
        closed.  When the market is open the reason is an empty string.
    """

    when = dt or datetime.now(NY_TZ)
    if when.tzinfo is None:
        when = when.replace(tzinfo=NY_TZ)
    else:
        when = when.astimezone(NY_TZ)

    weekday = when.weekday()
    if weekday >= 5:
        return False, "周末休市"

    calendar = _ensure_nyse_calendar()
    if calendar is not None:
        try:
            schedule = calendar.schedule(start_date=when.date(), end_date=when.date())
        except Exception:  # pragma: no cover - fallback path
            schedule = None
        if schedule is None or schedule.empty:
            return False, "节假日休市"

        market_open = schedule.iloc[0]["market_open"].tz_convert(NY_TZ)
        market_close = schedule.iloc[0]["market_close"].tz_convert(NY_TZ)
        if when < market_open:
            return False, "盘前休市"
        if when > market_close:
            return False, "盘后休市"
        return True, ""

    # Fallback when pandas-market-calendars isn't available.
    open_time = when.replace(hour=9, minute=30, second=0, microsecond=0)
    close_time = when.replace(hour=16, minute=0, second=0, microsecond=0)
    if when < open_time:
        return False, "盘前休市"
    if when > close_time:
        return False, "盘后休市"
    return True, ""


def is_us_market_open(dt: Optional[datetime] = None) -> bool:
    """Convenience wrapper returning the market open flag only."""

    is_open, _reason = get_us_market_session(dt)
    return is_open


# Other helpers --------------------------------------------------------------

def slugify_strategy_name(name: str) -> str:
    """Convert arbitrary text to a safe snake_case strategy name."""
    if not isinstance(name, str):
        return ""
    slug = re.sub(r"[^0-9a-zA-Z]+", "_", name.lower())
    slug = re.sub(r"_+", "_", slug)
    return slug.strip("_")


# Environment helpers --------------------------------------------------------

_ENABLED_VALUES = {"1", "true", "yes", "on"}


def _flag_enabled(value: Optional[str]) -> bool:
    if value is None:
        return False
    return value.strip().lower() in _ENABLED_VALUES


def is_running_tests() -> bool:
    """Detect whether the current process is executing under a test runner."""

    # pytest sets PYTEST_CURRENT_TEST for each running test.
    if os.getenv("PYTEST_CURRENT_TEST"):
        return True

    # ``pytest`` or ``unittest`` being imported is a strong indicator that
    # we're in a testing context (useful for subprocesses started by pytest).
    modules = sys.modules
    return "pytest" in modules or "unittest" in modules


def is_trading_disabled() -> bool:
    """Return ``True`` when order execution should be prevented.

    Besides the explicit ``IBBOT_DISABLE_TRADING`` flag we also automatically
    disable trading whenever the application is launched from a unit-testing
    context.  This guards against accidental real-money executions triggered
    by test suites.
    """

    if _flag_enabled(os.getenv("IBBOT_DISABLE_TRADING")):
        return True

    return is_running_tests()

