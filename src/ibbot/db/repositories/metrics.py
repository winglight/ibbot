"""Repository for writing service metric samples."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.ibbot.logging import get_logger

from ..schemas import ServiceMetric

__all__ = ["ServiceMetricRepository"]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


def _normalise_details(details: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if details is None:
        return None
    payload: dict[str, Any] = {}
    for key, value in details.items():
        if isinstance(value, datetime):
            payload[str(key)] = value.isoformat()
        elif isinstance(value, Mapping):
            nested = _normalise_details(value)
            payload[str(key)] = nested or {}
        elif isinstance(value, (list, tuple, set)):
            payload[str(key)] = [
                item.isoformat() if isinstance(item, datetime) else item for item in value
            ]
        else:
            payload[str(key)] = value
    return payload or None


def _as_row(entry: Mapping[str, Any]) -> dict[str, Any]:
    service = str(entry["service"]).strip()
    metric = str(entry["metric"]).strip()
    value = entry.get("value")
    if not service or not metric:
        raise ValueError("Service metric entry must include non-empty service and metric")
    if value is None:
        raise ValueError("Service metric entry missing value")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric_value = float(value)
    elif isinstance(value, str):
        numeric_value = float(value)
    else:
        raise ValueError("Metric value must be numeric")

    recorded_at = entry.get("recorded_at")
    row = {
        "service": service,
        "metric": metric,
        "value": numeric_value,
        "status": _coerce_str(entry.get("status")),
        "host": _coerce_str(entry.get("host")),
        "details": _normalise_details(entry.get("details")),
        "recorded_at": _parse_datetime(recorded_at) or _utc_now(),
    }
    return row


class ServiceMetricRepository:
    """Persist metrics in bulk or individually."""

    def __init__(self, session: AsyncSession, *, logger_name: str = "db.metrics") -> None:
        self._session = session
        self._logger = get_logger(logger_name)

    async def record_metrics(
        self,
        metrics: Mapping[str, Any] | Iterable[Mapping[str, Any]],
    ) -> None:
        if isinstance(metrics, Mapping):
            rows: Sequence[dict[str, Any]] = (_as_row(metrics),)
        else:
            rows = tuple(_as_row(entry) for entry in metrics)
        if not rows:
            return
        try:
            await self._session.execute(insert(ServiceMetric), rows)
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            self._logger.exception("Failed to record service metrics")
            raise
