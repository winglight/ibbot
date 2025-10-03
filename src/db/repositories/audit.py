"""DB-API powered audit log repository."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from src.db.async_support import run_in_thread
from src.db.mariadb import MariaDBConfigurationError
from src.ibbot.logging import get_logger

from .base import RepositoryError, RepositoryValidationError, execute_write, is_sqlite, json_dumps, use_connection

__all__ = ["AuditLogRepository", "AuditLogRepositoryError"]


class AuditLogRepositoryError(RepositoryError):
    """Raised when persisting audit log entries fails."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_text(value: Any, *, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        text = value.strip()
        return text or default
    return str(value)


def _normalise_payload(extra: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not extra:
        return None
    payload: dict[str, Any] = {}
    for key, value in extra.items():
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            payload[str(key)] = value
        elif isinstance(value, Mapping):
            nested = _normalise_payload(value)
            payload[str(key)] = nested or {}
        else:
            payload[str(key)] = str(value)
    return payload or None


class AuditLogRepository:
    """Persist audit entries into the ``audit_logs`` table."""

    def __init__(self, *, logger_name: str = "db.audit") -> None:
        self._logger = get_logger(logger_name)

    async def record_event(self, payload: Mapping[str, Any]) -> None:
        action = _coerce_text(payload.get("action"))
        if not action:
            raise RepositoryValidationError("Audit log entry requires an action")

        row = {
            "action": action,
            "result": _coerce_text(payload.get("result"), default="unknown"),
            "user": _coerce_text(payload.get("user"), default="anonymous"),
            "role": _coerce_text(payload.get("role"), default="unknown"),
            "method": _coerce_text(payload.get("method")) or None,
            "path": _coerce_text(payload.get("path")) or None,
            "detail": payload.get("detail") or None,
            "extra": _normalise_payload(payload.get("extra")),
            "created_at": payload.get("created_at") or _utc_now(),
        }

        try:
            await run_in_thread(self._write_event, row)
        except RepositoryValidationError:
            raise
        except MariaDBConfigurationError as exc:
            raise AuditLogRepositoryError(str(exc)) from exc
        except Exception as exc:  # pragma: no cover - log and wrap
            self._logger.exception("Failed to record audit log entry", action=action)
            raise AuditLogRepositoryError("Failed to record audit log entry") from exc

    def _write_event(self, row: Mapping[str, Any]) -> None:
        with use_connection() as connection:
            payload = dict(row)
            if payload.get("extra") is not None:
                payload["extra"] = json_dumps(payload["extra"])

            columns = list(payload.keys())
            placeholders = (
                ", ".join(f":{col}" for col in columns)
                if is_sqlite(connection)
                else ", ".join(f"%({col})s" for col in columns)
            )
            sql = "INSERT INTO audit_logs ({cols}) VALUES ({values})".format(
                cols=", ".join(columns), values=placeholders
            )
            execute_write(connection, sql, payload)
