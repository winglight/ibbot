"""Repository for writing service metric samples using DB-API."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from src.db.async_support import run_in_thread
from src.db.mariadb import MariaDBConfigurationError
from src.ibbot.logging import get_logger

from .base import (
    RepositoryError,
    RepositoryValidationError,
    executemany_write,
    is_sqlite,
    json_dumps,
    use_connection,
)

__all__ = ["ServiceMetricRepository", "ServiceMetricRepositoryError"]


class ServiceMetricRepositoryError(RepositoryError):
    """Raised when persisting service metrics fails."""


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
        raise RepositoryValidationError(
            "Service metric entry must include non-empty service and metric"
        )
    if value is None:
        raise RepositoryValidationError("Service metric entry missing value")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric_value = float(value)
    elif isinstance(value, str):
        numeric_value = float(value)
    else:
        raise RepositoryValidationError("Metric value must be numeric")

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
    """Persist metrics in bulk or individually using DB-API."""

    def __init__(self, *, logger_name: str = "db.metrics") -> None:
        self._logger = get_logger(logger_name)

    async def close(self) -> None:
        return None

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
            await run_in_thread(self._write_metrics, rows)
        except RepositoryValidationError:
            raise
        except MariaDBConfigurationError as exc:
            raise ServiceMetricRepositoryError(str(exc)) from exc
        except Exception as exc:  # pragma: no cover - log and wrap
            self._logger.exception("Failed to record service metrics", count=len(rows))
            raise ServiceMetricRepositoryError("Failed to record service metrics") from exc

    def _write_metrics(self, rows: Sequence[Mapping[str, Any]]) -> None:
        with use_connection() as connection:
            prepared_rows = []
            for row in rows:
                payload = dict(row)
                if payload.get("details") is not None:
                    payload["details"] = json_dumps(payload["details"])
                prepared_rows.append(payload)

            if not prepared_rows:
                return

            columns = list(prepared_rows[0].keys())
            placeholders = (
                ", ".join(f":{col}" for col in columns)
                if is_sqlite(connection)
                else ", ".join(f"%({col})s" for col in columns)
            )
            sql = "INSERT INTO service_metrics ({cols}) VALUES ({values})".format(
                cols=", ".join(columns), values=placeholders
            )
            executemany_write(connection, sql, prepared_rows)
