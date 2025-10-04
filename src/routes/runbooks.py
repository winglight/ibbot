"""HTTP routes exposing operational runbooks."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from src.runbooks import runbook_manager
from src.runbooks.schemas import (
    ErrorResponse,
    RunbookDetailResponse,
    RunbookListResponse,
)


router = APIRouter(prefix="/api/runbooks", tags=["runbooks"])


@router.get("", response_model=RunbookListResponse)
async def list_runbooks() -> RunbookListResponse:
    runbooks = await runbook_manager.list_runbooks()
    return RunbookListResponse(runbooks=runbooks)


@router.get(
    "/{runbook_id}",
    response_model=RunbookDetailResponse,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
)
async def get_runbook(runbook_id: str) -> RunbookDetailResponse:
    try:
        runbook = await runbook_manager.get_runbook(runbook_id)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="runbook not found")
    return RunbookDetailResponse(runbook=runbook)


@router.post(
    "/{runbook_id}/start",
    response_model=RunbookDetailResponse,
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
        status.HTTP_409_CONFLICT: {"model": ErrorResponse},
    },
)
async def start_runbook(runbook_id: str) -> RunbookDetailResponse:
    try:
        runbook = await runbook_manager.start_runbook(runbook_id)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="runbook not found")
    except RuntimeError as exc:
        if str(exc) == "runbook_running":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="runbook already running") from exc
        raise
    return RunbookDetailResponse(runbook=runbook)


__all__ = ["router"]

