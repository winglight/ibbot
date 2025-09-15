"""Utility helpers used across the project.

This module now also contains timezone conversion helpers so that all
server responses can produce local timestamps that correctly account for
US/Eastern daylight saving time when converting from IBKR data.
"""

import re
from datetime import datetime
from typing import Any

import pandas as pd
from zoneinfo import ZoneInfo


# Timezone helpers -----------------------------------------------------------
#
# IBKR timestamps are provided in US/Eastern time (America/New_York).  When
# converting these to the server's local timezone we need to be mindful of
# daylight saving time.  The ``to_local_time`` helper performs this
# conversion and returns an ISO formatted string with the correct offset.

LOCAL_TZ = datetime.now().astimezone().tzinfo
NY_TZ = ZoneInfo("America/New_York")


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
        return dt.astimezone(LOCAL_TZ).isoformat()
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

