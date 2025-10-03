"""Repository for persisting market events."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.ibbot.logging import get_logger

from ..schemas import MarketEvent

__all__ = ["MarketEventRepository"]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(ts_ns: Any, fallback: Any = None) -> datetime | None:
    if isinstance(ts_ns, datetime):
        if ts_ns.tzinfo is None:
            return ts_ns.replace(tzinfo=timezone.utc)
        return ts_ns
    if isinstance(ts_ns, (int, float)):
        if ts_ns > 1_000_000_000_000:
            return datetime.fromtimestamp(ts_ns / 1_000_000_000, tz=timezone.utc)
        return datetime.fromtimestamp(ts_ns, tz=timezone.utc)
    if isinstance(ts_ns, str) and ts_ns:
        try:
            parsed = datetime.fromisoformat(ts_ns)
        except ValueError:
            parsed = None
        else:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        if parsed is not None:
            return parsed
    if fallback is not None:
        return _parse_timestamp(fallback)
    return None


def _normalise_mapping(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    normalised: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, datetime):
            normalised[str(key)] = value.isoformat()
        elif isinstance(value, Mapping):
            nested = _normalise_mapping(value)
            normalised[str(key)] = nested or {}
        elif isinstance(value, (list, tuple, set)):
            normalised[str(key)] = [
                item.isoformat() if isinstance(item, datetime) else item for item in value
            ]
        else:
            normalised[str(key)] = value
    return normalised or None


def _ensure_mapping(event: Any) -> Mapping[str, Any]:
    if isinstance(event, Mapping):
        return event
    if is_dataclass(event):
        return asdict(event)
    if hasattr(event, "_asdict"):
        candidate = event._asdict()  # type: ignore[attr-defined]
        if isinstance(candidate, Mapping):
            return candidate
    raise TypeError("Unsupported event payload")


class MarketEventRepository:
    """Persist market events emitted by the data layer."""

    def __init__(self, session: AsyncSession, *, logger_name: str = "db.events") -> None:
        self._session = session
        self._logger = get_logger(logger_name)

    async def insert_event(self, event: Mapping[str, Any] | Any) -> None:
        data = _ensure_mapping(event)
        payload = _normalise_mapping(data.get("payload")) or {}
        meta = _normalise_mapping(data.get("meta"))
        timestamp = _parse_timestamp(data.get("ts_ns"), data.get("timestamp")) or _utc_now()
        row = {
            "event_type": str(data.get("event_type") or ""),
            "topic": str(data.get("topic") or ""),
            "symbol": data.get("symbol"),
            "source": data.get("source"),
            "payload": payload,
            "meta": meta,
            "event_time": timestamp,
            "ingested_at": _utc_now(),
        }
        if not row["event_type"] or not row["topic"]:
            raise ValueError("Event must provide event_type and topic")
        try:
            await self._session.execute(insert(MarketEvent).values(**row))
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            self._logger.exception(
                "Failed to persist market event", event_type=row["event_type"], topic=row["topic"]
            )
            raise
