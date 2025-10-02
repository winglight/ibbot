"""Helpers for interpreting ib-command service health payloads."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, MutableMapping


__all__ = [
    "IB_COMMAND_HEALTH_KEY",
    "IB_COMMAND_HEALTH_POLL_INTERVAL_SEC",
    "IB_COMMAND_HEALTH_STALE_AFTER_SEC",
    "resolve_ib_account_type",
    "build_ib_connection_health_payload",
]


IB_COMMAND_HEALTH_KEY = "hash:service:health:ib-command"


def _get_interval_env(name: str, default: int) -> int:
    """Read an integer interval (seconds) from the environment."""

    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(0, value)


IB_COMMAND_HEALTH_POLL_INTERVAL_SEC = _get_interval_env(
    "IB_COMMAND_HEALTH_POLL_INTERVAL_SEC", 5
)
IB_COMMAND_HEALTH_STALE_AFTER_SEC = _get_interval_env(
    "IB_COMMAND_HEALTH_STALE_AFTER_SEC", 15
)


def resolve_ib_account_type() -> str:
    """Resolve the IB account type label for UI display."""

    override = os.getenv("IB_COMMAND_ACCOUNT_TYPE") or os.getenv("IB_ACCOUNT_TYPE")
    if override:
        cleaned = override.strip()
        if cleaned:
            return cleaned.title()

    flag = os.getenv("IBKR_PAPER_TRADING", "true").strip().lower()
    if flag in {"0", "false", "no", "live"}:
        return "Live"
    return "Paper"


@dataclass(slots=True)
class IBCommandHealth:
    """Normalised representation of the ib-command Redis health payload."""

    connected: bool
    account_type: str
    last_seen: str | None
    queue_depth: int | None
    last_error: str | None
    error_code: int | None
    stale: bool
    source: str = "redis"

    def as_mapping(self) -> MutableMapping[str, Any]:
        """Return a mapping suitable for JSON responses."""

        return {
            "connected": self.connected,
            "account_type": self.account_type,
            "last_seen": self.last_seen,
            "queue_depth": self.queue_depth,
            "last_error": self.last_error,
            "error_code": self.error_code,
            "stale": self.stale,
            "source": self.source,
        }


def build_ib_connection_health_payload(
    raw: Mapping[str, str] | None,
    *,
    stale_after: float | None = None,
    account_type: str | None = None,
) -> MutableMapping[str, Any]:
    """Convert raw Redis values into a connection status mapping."""

    if raw is None:
        raw = {}

    now = datetime.now(timezone.utc)
    last_seen_raw = raw.get("last_seen", "")
    last_seen_dt: datetime | None = None
    age_seconds: float | None = None

    if last_seen_raw:
        try:
            last_seen_dt = datetime.fromisoformat(last_seen_raw)
        except ValueError:
            last_seen_dt = None
        else:
            delta = now - last_seen_dt
            age_seconds = max(delta.total_seconds(), 0.0)

    threshold = stale_after if stale_after and stale_after > 0 else 30.0
    stale = age_seconds is None or age_seconds > threshold

    queue_depth_raw = raw.get("queue_depth")
    queue_depth: int | None = None
    if queue_depth_raw is not None:
        try:
            queue_depth = int(queue_depth_raw)
        except (TypeError, ValueError):
            queue_depth = None

    last_error_raw = raw.get("last_error")
    last_error = last_error_raw.strip() if last_error_raw else ""

    ib_connected_raw = raw.get("ib_connected")

    def _coerce_bool(value: Any) -> bool | None:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            cleaned = value.strip().lower()
            if not cleaned:
                return None
            if cleaned in {"1", "true", "yes", "y", "on"}:
                return True
            if cleaned in {"0", "false", "no", "n", "off"}:
                return False
        return None

    ib_connected = _coerce_bool(ib_connected_raw) if ib_connected_raw is not None else None

    connection_error_raw = raw.get("ib_connection_error")
    connection_error = connection_error_raw.strip() if connection_error_raw else ""
    if connection_error:
        last_error = connection_error

    if not last_error:
        last_error = None

    error_code_raw = raw.get("last_error_code")
    error_code: int | None = None
    if error_code_raw not in (None, ""):
        try:
            error_code = int(error_code_raw)
        except (TypeError, ValueError):
            error_code = None

    has_error = last_error is not None or error_code is not None

    if ib_connected is not None:
        connected = bool(ib_connected) and not stale
    else:
        connected = (
            not stale
            and not has_error
            and queue_depth is not None
            and queue_depth >= 0
        )

    payload = IBCommandHealth(
        connected=connected,
        account_type=account_type or resolve_ib_account_type(),
        last_seen=last_seen_dt.isoformat() if last_seen_dt else None,
        queue_depth=queue_depth,
        last_error=last_error,
        error_code=error_code,
        stale=stale,
    )
    return payload.as_mapping()
