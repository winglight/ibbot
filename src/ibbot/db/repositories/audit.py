"""Repository for persisting audit trail records."""

from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.ibbot.logging import get_logger

from ..schemas import AuditLog

__all__ = ["AuditLogRepository"]


class AuditLogRepository:
    """Persist audit events in MariaDB."""

    def __init__(self, session: AsyncSession, *, logger_name: str = "db.audit") -> None:
        self._session = session
        self._logger = get_logger(logger_name)

    async def insert_entry(self, payload: Mapping[str, Any]) -> None:
        row = {
            "username": str(payload.get("username") or "anonymous"),
            "role": str(payload.get("role") or "unknown"),
            "action": str(payload.get("action") or "unknown"),
            "method": str(payload.get("method") or "UNKNOWN"),
            "path": str(payload.get("path") or "/"),
            "result": str(payload.get("result") or "unknown"),
            "detail": payload.get("detail"),
            "extra": payload.get("extra"),
        }

        if not row["username"]:
            row["username"] = "anonymous"
        if not row["role"]:
            row["role"] = "unknown"

        try:
            await self._session.execute(insert(AuditLog).values(**row))
        except Exception:
            await self._session.rollback()
            self._logger.exception("Failed to insert audit log entry")
            raise
