"""Pydantic schemas used by the runbook API."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel


TaskStatus = Literal["pending", "running", "success", "warning", "error"]
RunbookStatus = Literal["idle", "running", "success", "warning", "error"]


class RunbookTask(BaseModel):
    id: str
    title: str
    description: str
    status: TaskStatus
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    details: Optional[Dict[str, Any]] = None


class Runbook(BaseModel):
    id: str
    name: str
    description: str
    status: RunbookStatus
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    run_id: Optional[str] = None
    tasks: List[RunbookTask]


class RunbookListResponse(BaseModel):
    runbooks: List[Runbook]


class RunbookDetailResponse(BaseModel):
    runbook: Runbook


class ErrorResponse(BaseModel):
    detail: str


__all__ = [
    "ErrorResponse",
    "Runbook",
    "RunbookDetailResponse",
    "RunbookListResponse",
    "RunbookTask",
    "RunbookStatus",
    "TaskStatus",
]

