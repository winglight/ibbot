"""Audit logging utilities for security-sensitive operations."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping

from fastapi import Request
from loguru import logger
from sqlalchemy.exc import SQLAlchemyError

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
    """Persist an audit trail record via the MariaDB repositories.

    The function is intentionally tolerant: failures when writing to the database
    are logged at debug level but never propagated to the caller so that security
    checks do not disrupt the primary control flow.
    """

    username = getattr(request.state, "audit_username", None) or getattr(request.state, "username", None) or "anonymous"
    role = getattr(request.state, "audit_role", None) or getattr(request.state, "user_role", None) or (
        "visitor" if getattr(request.state, "is_read_only", False) else "unknown"
    )
    method = getattr(request.state, "audit_method", None) or request.method.upper()
    path = getattr(request.state, "audit_path", None) or request.url.path

    timestamp = datetime.now(timezone.utc)
    result = "success" if success else "failure"

    extra_fields = _coerce_fields(extra)

    entry = {
        "username": username,
        "role": role,
        "action": action,
        "method": method,
        "path": path,
        "result": result,
        "detail": detail,
        "extra": extra_fields or None,
    }

    try:
        async with session_scope() as session:
            repository = audit_log_repository(session)
            await repository.insert_entry(entry)
    except (DatabaseConfigError, SQLAlchemyError) as exc:
        logger.debug("Failed to persist audit log entry: {}", exc)
    except Exception as exc:  # pragma: no cover - defensive catch-all
        logger.debug("Unexpected error while writing audit log: {}", exc)
