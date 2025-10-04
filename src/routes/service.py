"""Routes for managing infrastructure services."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from loguru import logger
from pydantic import BaseModel, Field

from src.account_service_readiness import refresh_account_service_status
from src.service_status import RedisUnavailableError, build_service_status_payload

from .auth import audit_log_failure, audit_log_success, get_current_user, require_roles
from src.service_restart import (
    CommandExecutionError,
    RestartCommandResult,
    RestartMode,
    UnknownServiceError,
    get_service_restart_manager,
)


router = APIRouter()
_REDIS_UNAVAILABLE_DETAIL = {"error": "redis_unavailable", "message": "Redis 服务不可用"}


@router.on_event("startup")
async def _warm_account_service_status() -> None:
    """Perform an early readiness probe for the account service."""

    try:
        await refresh_account_service_status(max_age=None, log_failure=False)
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Account service readiness warmup failed: %s", exc)


@router.get("/api/services/status")
async def get_service_status() -> dict[str, object]:
    """Return the latest known status for core and auxiliary services."""

    try:
        payload = await build_service_status_payload()
    except RedisUnavailableError as exc:
        logger.warning("Redis unavailable for service status endpoint", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=dict(_REDIS_UNAVAILABLE_DETAIL),
        ) from exc
    return dict(payload)


class ServiceRestartPayload(BaseModel):
    service: str = Field(..., min_length=1, max_length=128)
    include_dependencies: bool = Field(default=True, description="Restart dependencies first")


def _serialise_result(result: RestartCommandResult) -> dict[str, object]:
    return {
        "service": result.service,
        "command": list(result.command),
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "mode": result.mode.value,
    }


async def _perform_restart(
    payload: ServiceRestartPayload,
    request: Request,
    *,
    mode: RestartMode | None,
) -> dict[str, object]:
    manager = get_service_restart_manager(mode=mode)
    try:
        results = await manager.restart(
            payload.service, include_dependencies=payload.include_dependencies
        )
    except UnknownServiceError as exc:
        await audit_log_failure(
            request,
            detail=f"unknown service: {payload.service}",
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except CommandExecutionError as exc:
        result = exc.result
        logger.error(
            "Service restart failed",
            service=result.service,
            command=" ".join(result.command),
            returncode=result.returncode,
            stderr=result.stderr,
        )
        await audit_log_failure(
            request,
            detail=f"restart failed for {result.service}",
            extra={"returncode": result.returncode},
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to restart {result.service}",
        ) from exc
    except FileNotFoundError as exc:
        logger.exception("Restart tooling not found")
        await audit_log_failure(request, detail="restart tooling not installed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Restart tooling is not available on the server",
        ) from exc
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("Unexpected error during service restart")
        await audit_log_failure(request, detail="unexpected error during restart")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to restart service",
        ) from exc

    restarted = [result.service for result in results]
    message = (
        f"已重启 {'、'.join(restarted)}" if restarted else "服务重启完成"
    )

    await audit_log_success(
        request,
        detail=f"restarted {', '.join(restarted) if restarted else payload.service}",
        extra={"services": restarted or [payload.service]},
    )

    return {
        "success": True,
        "service": payload.service,
        "restarted": restarted,
        "message": message,
        "logs": [_serialise_result(result) for result in results],
        "mode": manager.mode.value,
    }


@router.post("/api/services/restart")
async def restart_service(
    payload: ServiceRestartPayload,
    request: Request,
    _user: str | None = Depends(require_roles("admin", action="services.restart")),
) -> dict[str, object]:
    """Restart a managed service using the auto-detected controller."""

    return await _perform_restart(payload, request, mode=None)


@router.post("/api/services/restart/local")
async def restart_service_local(
    payload: ServiceRestartPayload,
    request: Request,
    _user: str | None = Depends(require_roles("admin", action="services.restart")),
) -> dict[str, object]:
    """Restart a managed service using the local supervisor controller."""

    return await _perform_restart(payload, request, mode=RestartMode.LOCAL)

