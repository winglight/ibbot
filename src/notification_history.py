from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping

DEFAULT_HISTORY_KEY = "list:notification:history"
DEFAULT_HISTORY_LIMIT = 500


class NotificationHistoryError(Exception):
    """Base error for notification history helpers."""


class InvalidHistoryLimitError(NotificationHistoryError):
    """Raised when the requested limit is not a valid integer."""

    def __init__(self, value: Any) -> None:
        super().__init__("limit must be an integer")
        self.value = value


class InvalidHistoryTimestampError(NotificationHistoryError):
    """Raised when the provided timestamp cannot be parsed."""

    def __init__(self, value: str) -> None:
        super().__init__(f"Invalid timestamp: {value}")
        self.value = value


def parse_history_timestamp(value: str | None) -> datetime | None:
    """Normalise a timestamp string to a UTC datetime."""

    if not value:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:  # pragma: no cover - defensive guard
        raise InvalidHistoryTimestampError(value) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalise_filter(value: str | None) -> str | None:
    if isinstance(value, str):
        candidate = value.strip().lower()
        if candidate:
            return candidate
    return None


def _parse_entry_timestamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    candidate = raw.strip()
    if not candidate:
        return None
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


async def fetch_notification_history(
    redis: Any,
    *,
    history_key: str = DEFAULT_HISTORY_KEY,
    history_limit: int | None = DEFAULT_HISTORY_LIMIT,
    channel: str | None = None,
    status: str | None = None,
    since: str | None = None,
    limit: int = 100,
) -> Mapping[str, Any]:
    """Fetch notification history records from Redis."""

    try:
        limit_value = max(1, int(limit))
    except (TypeError, ValueError) as exc:
        raise InvalidHistoryLimitError(limit) from exc

    history_cap = max(0, int(history_limit)) if isinstance(history_limit, int) else 0
    if history_cap:
        limit_value = min(limit_value, history_cap)
        fetch_end = max(history_cap - 1, 0)
    else:
        fetch_end = max(limit_value * 5 - 1, limit_value - 1)

    raw_entries = await redis.lrange(history_key, 0, fetch_end)

    parsed_since = parse_history_timestamp(since)
    channel_filter = _normalise_filter(channel)
    status_filter = _normalise_filter(status)

    history: list[Mapping[str, Any]] = []
    for raw in raw_entries:
        if not isinstance(raw, str):
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if channel_filter and str(entry.get("channel", "")).lower() != channel_filter:
            continue
        if status_filter and str(entry.get("status", "")).lower() != status_filter:
            continue
        if parsed_since:
            entry_time = _parse_entry_timestamp(entry.get("timestamp"))
            if entry_time is not None and entry_time < parsed_since:
                continue
        history.append(entry)
        if len(history) >= limit_value:
            break

    return {"success": True, "history": history}


async def clear_notification_history(
    redis: Any,
    *,
    history_key: str = DEFAULT_HISTORY_KEY,
) -> Mapping[str, Any]:
    """Remove all notification history records from Redis."""

    removed = await redis.delete(history_key)
    return {"success": True, "cleared": int(removed or 0)}
