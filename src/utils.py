"""Utility helpers used across the project.

This module centralises timezone conversions so that all timestamps stored in
or returned by the application consistently use the Asia/Shanghai (+08:00)
timezone.  IBKR data continues to arrive in US/Eastern and therefore requires
conversion before being surfaced to clients.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

import pandas as pd
from zoneinfo import ZoneInfo


# Timezone helpers -----------------------------------------------------------
#
# IBKR timestamps are provided in US/Eastern time (America/New_York).  When
# converting these to the server's local timezone we need to be mindful of
# daylight saving time.  ``LOCAL_TZ`` is fixed to Asia/Shanghai so that the
# backend behaves consistently regardless of the host machine's timezone.

LOCAL_TZ = ZoneInfo("Asia/Shanghai")
NY_TZ = ZoneInfo("America/New_York")


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


# Other helpers --------------------------------------------------------------

def slugify_strategy_name(name: str) -> str:
    """Convert arbitrary text to a safe snake_case strategy name."""
    if not isinstance(name, str):
        return ""
    slug = re.sub(r"[^0-9a-zA-Z]+", "_", name.lower())
    slug = re.sub(r"_+", "_", slug)
    return slug.strip("_")

