"""Repository helpers for persisting market events to parquet storage."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from src.db.async_support import run_in_thread
from src.ibbot.logging import get_logger
from src.ibbot.storage.parquet_writer import MarketDataParquetWriter, get_market_data_writer

from .base import RepositoryError, RepositoryValidationError

__all__ = ["MarketEventRepository", "MarketEventRepositoryError"]


class MarketEventRepositoryError(RepositoryError):
    """Raised when persisting market events fails."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: Any, fallback: Any = None) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, (int, float)):
        if value > 1_000_000_000_000:
            return datetime.fromtimestamp(value / 1_000_000_000, tz=timezone.utc)
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
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
    raise RepositoryValidationError("Unsupported event payload")


def _normalise_ts_ns(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


class MarketEventRepository:
    """Persist market events emitted by the data layer."""

    def __init__(
        self,
        *,
        logger_name: str = "db.events",
        writer: MarketDataParquetWriter | None = None,
    ) -> None:
        self._logger = get_logger(logger_name)
        self._writer = writer or get_market_data_writer()

    async def insert_event(self, event: Mapping[str, Any] | Any) -> None:
        data = _ensure_mapping(event)
        payload = _normalise_mapping(data.get("payload")) or {}
        meta = _normalise_mapping(data.get("meta"))
        ts_ns = _normalise_ts_ns(data.get("ts_ns"))
        timestamp = (
            _parse_timestamp(data.get("recorded_at"), ts_ns)
            or _parse_timestamp(data.get("timestamp"), ts_ns)
            or _utc_now()
        )
        event_type = str(data.get("event_type") or "")
        topic = str(data.get("topic") or "")
        if not event_type or not topic:
            raise RepositoryValidationError("Event must provide event_type and topic")

        try:
            await run_in_thread(
                self._persist_event,
                data,
                payload,
                meta,
                timestamp,
                ts_ns,
                event_type,
            )
        except RepositoryValidationError:
            raise
        except Exception as exc:  # pragma: no cover - log and wrap
            self._logger.exception(
                "Failed to persist market event",
                event_type=event_type,
                topic=topic,
            )
            raise MarketEventRepositoryError("Failed to persist market event") from exc

    def _persist_event(
        self,
        data: Mapping[str, Any],
        payload: Mapping[str, Any],
        meta: Mapping[str, Any] | None,
        timestamp: datetime,
        ts_ns: int | None,
        event_type: str,
    ) -> None:
        if event_type == "market.candle":
            self._persist_candle_event(data, payload, meta, timestamp, ts_ns)
            return

        self._logger.debug(
            "No persistence backend configured for event type", event_type=event_type
        )

    def _persist_candle_event(
        self,
        data: Mapping[str, Any],
        payload: Mapping[str, Any],
        meta: Mapping[str, Any] | None,
        timestamp: datetime,
        ts_ns: int | None,
    ) -> None:
        writer = self._writer
        if writer is None:
            raise RepositoryValidationError("Parquet writer is not available")

        symbol = (
            data.get("symbol")
            or payload.get("symbol")
            or (meta or {}).get("symbol")
        )
        interval = payload.get("interval") or (meta or {}).get("interval")
        if not symbol or not interval:
            raise RepositoryValidationError("Candle event requires symbol and interval")

        enriched_payload = dict(payload)
        enriched_payload.setdefault("recorded_at", timestamp.isoformat())
        enriched_payload.setdefault("ingested_at", _utc_now().isoformat())

        writer.append_candle(
            str(symbol),
            str(interval),
            enriched_payload,
            timestamp_ns=ts_ns,
        )
