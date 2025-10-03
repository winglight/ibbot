from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger

from .auth import get_current_user

router = APIRouter(prefix="/api/services")


def _get_module_registry(request: Request):
    registry = getattr(request.app.state, "module_registry", None)
    if registry is None:
        raise HTTPException(status_code=503, detail="Module registry unavailable")
    return registry


@router.get("", dependencies=[Depends(get_current_user)])
async def list_services(request: Request):
    registry = _get_module_registry(request)
    return {"services": registry.service_metadata()}


@router.get("/health")
async def service_health(request: Request, user: Optional[str] = Depends(get_current_user)):
    registry = _get_module_registry(request)
    return {"services": registry.health_snapshot()}


@router.post("/{module}/restart")
async def restart_service(module: str, request: Request, user: Optional[str] = Depends(get_current_user)):
    registry = _get_module_registry(request)
    try:
        handle = await registry.restart_module(module)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown service: {module}")
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        logger.warning(f"Service restart failed for {module}: {exc}")
        raise HTTPException(status_code=500, detail=f"Failed to restart {module}")

    return {"success": True, "module": handle.name}
