"""Routes for managing infrastructure services."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from loguru import logger
from pydantic import BaseModel, Field

from .auth import get_current_user
from src.service_restart import (
    DockerExecutionError,
    DockerCommandResult,
    UnknownServiceError,
    get_service_restart_manager,
)


router = APIRouter()


class ServiceRestartPayload(BaseModel):
    service: str = Field(..., min_length=1, max_length=128)
    include_dependencies: bool = Field(default=True, description="Restart dependencies first")


def _serialise_result(result: DockerCommandResult) -> dict[str, object]:
    return {
        "service": result.service,
        "command": list(result.command),
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


@router.post("/api/services/restart")
async def restart_service(
    payload: ServiceRestartPayload,
    request: Request,
    _user: str | None = Depends(get_current_user),
) -> dict[str, object]:
    """Restart a docker-compose managed service and its dependencies."""

    if getattr(request.state, "is_read_only", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Read-only accounts cannot restart services",
        )

    manager = get_service_restart_manager()
    try:
        results = await manager.restart(
            payload.service, include_dependencies=payload.include_dependencies
        )
    except UnknownServiceError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DockerExecutionError as exc:
        result = exc.result
        logger.error(
            "Service restart failed",
            service=result.service,
            command=" ".join(result.command),
            returncode=result.returncode,
            stderr=result.stderr,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to restart {result.service}",
        ) from exc
    except FileNotFoundError as exc:
        logger.exception("Docker compose command not found")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Docker is not available on the server",
        ) from exc
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("Unexpected error during service restart")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to restart service",
        ) from exc

    restarted = [result.service for result in results]
    message = (
        f"已重启 {'、'.join(restarted)}" if restarted else "服务重启完成"
    )

    return {
        "success": True,
        "service": payload.service,
        "restarted": restarted,
        "message": message,
        "logs": [_serialise_result(result) for result in results],
    }

