"""Audit logging utilities for security-sensitive operations."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from fastapi import Request
from loguru import logger

from src.db import MariaDBConfigurationError, connect
from src.db.async_support import run_in_thread
from src.db.repositories.base import execute_write, is_sqlite, json_dumps

__all__ = ["DatabaseAuditLogWriter", "create_audit_log_writer", "log_audit_event"]


def _coerce_fields(extra: Mapping[str, Any] | None) -> dict[str, Any]:
    if not extra:
        return {}

    coerced: dict[str, Any] = {}
    for key, value in extra.items():
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            coerced[str(key)] = value
            continue
        try:
            coerced[str(key)] = json_dumps(value)
        except TypeError:
            coerced[str(key)] = str(value)
    return coerced


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        candidate = value.strip()
    else:
        candidate = str(value).strip()
    return candidate or None


def _required_text(value: Any, *, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        candidate = value.strip()
    else:
        candidate = str(value).strip()
    return candidate or default


def _build_audit_row(payload: Mapping[str, Any]) -> dict[str, Any]:
    action = _required_text(payload.get("action"))
    if not action:
        raise ValueError("Audit log entry requires an action")

    extra = _coerce_fields(payload.get("extra"))
    row: dict[str, Any] = {
        "action": action,
        "result": _required_text(payload.get("result"), default="unknown"),
        "user": _required_text(payload.get("user"), default="anonymous"),
        "role": _required_text(payload.get("role"), default="unknown"),
        "method": _optional_text(payload.get("method")),
        "path": _optional_text(payload.get("path")),
        "detail": _optional_text(payload.get("detail")) or "",
        "extra": extra or None,
        "created_at": payload.get("created_at") or datetime.now(timezone.utc),
    }
    return row


def _write_audit_row(row: Mapping[str, Any]) -> None:
    with connect() as connection:
        payload = dict(row)
        if payload.get("extra"):
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


class DatabaseAuditLogWriter:
    """Persist audit log events into the ``audit_logs`` table."""

    async def record_event(self, payload: Mapping[str, Any]) -> None:
        row = _build_audit_row(payload)
        await run_in_thread(_write_audit_row, row)


def create_audit_log_writer() -> DatabaseAuditLogWriter:
    """Return a writer capable of persisting audit log entries."""

    return DatabaseAuditLogWriter()


async def log_audit_event(
    request: Request,
    *,
    action: str,
    success: bool,
    detail: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> None:
    """Write an audit log entry describing the current request.

    Database errors are logged and the event is emitted through the logger so
    that the primary application flow is not disrupted.
    """

    username = (
        getattr(request.state, "audit_username", None)
        or getattr(request.state, "username", None)
        or "anonymous"
    )
    role = (
        getattr(request.state, "audit_role", None)
        or getattr(request.state, "user_role", None)
        or ("visitor" if getattr(request.state, "is_read_only", False) else "unknown")
    )
    method = getattr(request.state, "audit_method", None) or request.method.upper()
    path = getattr(request.state, "audit_path", None) or request.url.path

    writer = create_audit_log_writer()
    payload = {
        "action": action,
        "result": "success" if success else "failure",
        "user": username,
        "role": role,
        "method": method,
        "path": path,
        "detail": detail or "",
        "extra": _coerce_fields(extra),
    }

    try:
        await writer.record_event(payload)
    except MariaDBConfigurationError as exc:
        logger.warning("Audit log persistence error: %s", exc)
        logger.bind(
            action=action,
            result=payload["result"],
            user=username,
            role=role,
            method=method,
            path=path,
        ).info("AUDIT (fallback): %s", detail or "")
    except Exception as exc:  # pragma: no cover - defensive safety net
        logger.debug("Failed to persist audit log: %s", exc)
        logger.bind(
            action=action,
            result=payload["result"],
            user=username,
            role=role,
            method=method,
            path=path,
        ).info("AUDIT (fallback): %s", detail or "")
