"""Lightweight readiness probe for the account microservice.

The main FastAPI application depends on ``services.account`` to serve account
information snapshots over HTTP.  Historically the websocket handshake would
abort when the dependency was missing which made troubleshooting difficult.
This helper keeps track of whether the account service is reachable, exposes
the latest probe result for status endpoints, and ensures we only log a single
warning when the dependency is offline.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Dict

from loguru import logger

from src.routes.service_clients import (
    AccountServiceClient,
    ServiceClientError,
    ServiceUnavailableError,
)


__all__ = [
    "AccountServiceReadiness",
    "get_account_service_status",
    "refresh_account_service_status",
    "probe_account_service",
]


@dataclass(slots=True)
class AccountServiceReadiness:
    """Container storing the latest readiness probe outcome."""

    ready: bool
    checked_at: datetime | None = None
    error: str | None = None

    def as_payload(self) -> Dict[str, object]:
        checked_iso = (
            self.checked_at.astimezone(timezone.utc).isoformat()
            if self.checked_at is not None
            else None
        )
        payload: Dict[str, object] = {
            "ready": self.ready,
            "checked_at": checked_iso,
        }
        if self.error:
            payload["error"] = self.error
        return payload


_CLIENT = AccountServiceClient()
_STATUS_LOCK = asyncio.Lock()
_STATUS = AccountServiceReadiness(ready=False, checked_at=None, error=None)
_WARNING_REPORTED = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def probe_account_service(*, log: bool = True) -> AccountServiceReadiness:
    """Ping the account service and update the cached readiness state."""

    global _STATUS, _WARNING_REPORTED

    now = _now()
    ready = False
    error_message: str | None = None

    try:
        await _CLIENT.fetch_info(force_refresh=False)
        ready = True
    except (ServiceUnavailableError, ServiceClientError) as exc:
        error_message = str(exc)
    except Exception as exc:  # pragma: no cover - defensive guard
        error_message = str(exc)

    new_status = AccountServiceReadiness(ready=ready, checked_at=now, error=error_message)

    async with _STATUS_LOCK:
        previous_status = _STATUS
        _STATUS = new_status

    if ready:
        if log and not previous_status.ready and _WARNING_REPORTED:
            logger.info("Account service readiness probe succeeded after previous failures")
        _WARNING_REPORTED = False
    else:
        if log and (previous_status.ready or not _WARNING_REPORTED):
            logger.warning(
                "Account service readiness probe failed: %s",
                error_message or "unknown error",
            )
            _WARNING_REPORTED = True

    return new_status


async def refresh_account_service_status(
    *, max_age: float | None = 60.0, log_failure: bool = True
) -> AccountServiceReadiness:
    """Ensure we have a recent readiness probe result.

    Args:
        max_age: Maximum acceptable age for the cached probe result (seconds).
            A value of ``None`` disables the cache and always performs a probe.
        log_failure: When ``True`` a warning is emitted the first time the
            dependency is unreachable until it recovers.
    """

    snapshot = _STATUS
    checked_at = snapshot.checked_at
    if max_age is not None and checked_at is not None:
        if (_now() - checked_at).total_seconds() < max_age:
            return replace(snapshot)

    return await probe_account_service(log=log_failure)


def get_account_service_status() -> AccountServiceReadiness:
    """Return a copy of the latest readiness probe result."""

    return replace(_STATUS)

