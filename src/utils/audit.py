"""Audit logging utilities for security-sensitive operations."""

from __future__ import annotations

import json
from typing import Any, Mapping

from fastapi import Request
from loguru import logger

from src.ibbot.db import DatabaseConfigError, audit_log_repository, session_scope


def _coerce_fields(extra: Mapping[str, Any] | None) -> dict[str, Any]:
    if not extra:
        return {}

    coerced: dict[str, Any] = {}
    for key, value in extra.items():
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            coerced[key] = value
        else:
            try:
                coerced[key] = json.dumps(value, ensure_ascii=False)
            except TypeError:
                coerced[key] = str(value)
    return coerced


async def log_audit_event(
    request: Request,
    *,
    action: str,
    success: bool,
    detail: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> None:
    """Write an audit trail record to MariaDB via the SQLAlchemy layer.

    The function is intentionally tolerant: database write failures are logged
    and a structured log entry is emitted instead so that security checks do
    not disrupt the primary control flow.
    """

    username = getattr(request.state, "audit_username", None) or getattr(request.state, "username", None) or "anonymous"
    role = getattr(request.state, "audit_role", None) or getattr(request.state, "user_role", None) or (
        "visitor" if getattr(request.state, "is_read_only", False) else "unknown"
    )
    method = getattr(request.state, "audit_method", None) or request.method.upper()
    path = getattr(request.state, "audit_path", None) or request.url.path

    result = "success" if success else "failure"

    try:
        async with session_scope() as session:
            repo = audit_log_repository(session)
            await repo.record_event(
                {
                    "action": action,
                    "result": result,
                    "user": username,
                    "role": role,
                    "method": method,
                    "path": path,
                    "detail": detail or "",
                    "extra": _coerce_fields(extra),
                }
            )
    except DatabaseConfigError as exc:
        logger.warning("Audit log database configuration error: {}", exc)
        logger.bind(
            action=action,
            result=result,
            user=username,
            role=role,
            method=method,
            path=path,
        ).info("AUDIT (fallback): {}", detail or "")
    except Exception as exc:  # pragma: no cover - safety net for production
        logger.debug("Failed to persist audit log: {}", exc)
        logger.bind(
            action=action,
            result=result,
            user=username,
            role=role,
            method=method,
            path=path,
        ).info("AUDIT (fallback): {}", detail or "")
