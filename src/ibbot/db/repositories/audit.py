"""Repository for persisting audit trail events."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.ibbot.logging import get_logger

from ..schemas import AuditLog

__all__ = ["AuditLogRepository"]


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
    """Persist audit entries into the MariaDB ``audit_logs`` table."""

    def __init__(self, session: AsyncSession, *, logger_name: str = "db.audit") -> None:
        self._session = session
        self._logger = get_logger(logger_name)

    async def record_event(self, payload: Mapping[str, Any]) -> None:
        action = _coerce_text(payload.get("action"))
        if not action:
            raise ValueError("Audit log entry requires an action")

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
            await self._session.execute(insert(AuditLog).values(**row))
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            self._logger.exception("Failed to record audit log entry", action=action)
            raise
