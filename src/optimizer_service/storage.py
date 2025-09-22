"""In-memory storage for optimizer tasks."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from typing import Dict, List, Optional, Tuple

from .schemas import (
    OptimizationReportData,
    OptimizationRequest,
    TaskState,
)
from .db import OptimizerDatabase, optimizer_db


@dataclass
class TaskRecord:
    request: OptimizationRequest
    state: TaskState = TaskState.PENDING
    progress: int = 0
    message: Optional[str] = None
    fail_reasons: List[str] = field(default_factory=list)
    report: Optional[OptimizationReportData] = None
    plan_id: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None


class OptimizerTaskStore:
    """Thread-safe in-memory task store.

    Designed for initial implementation; can later be replaced by persistent storage
    (e.g. database) without changing route handlers.
    """

    def __init__(self, database: OptimizerDatabase = optimizer_db) -> None:
        self._tasks: Dict[str, TaskRecord] = {}
        self._lock = Lock()
        self._db = database
        self._load_from_db()

    def create_task(
        self,
        task_id: str,
        request: OptimizationRequest,
        plan_id: Optional[str] = None,
        *,
        initial_state: TaskState = TaskState.PENDING,
        progress: int | None = None,
        message: Optional[str] = None,
    ) -> None:
        """Create a task record and persist it.

        ``PlanManager`` triggers tasks in the background. When observers query
        the reports immediately after a plan is started we want them to see the
        task as *running* rather than ``pending``. Otherwise there is a short
        window where the task is still pending even though execution has
        already been scheduled, which caused the tests to read a ``pending``
        state unexpectedly. By allowing the caller to specify the initial
        state we can mark plan triggered tasks as running right from the
        beginning while keeping the default behaviour (pending) for the public
        ``/api/optimize/start`` endpoint.
        """

        with self._lock:
            record = TaskRecord(
                request=request,
                state=initial_state,
                progress=max(0, min(100, progress)) if progress is not None else 0,
                message=message,
                plan_id=plan_id,
            )
            self._tasks[task_id] = record
            self._persist(task_id)

    def get_task(self, task_id: str) -> Optional[TaskRecord]:
        with self._lock:
            return self._tasks.get(task_id)

    def update_state(
        self,
        task_id: str,
        state: TaskState,
        progress: Optional[int] = None,
        message: Optional[str] = None,
    ) -> None:
        with self._lock:
            record = self._tasks.get(task_id)
            if not record:
                return
            record.state = state
            if progress is not None:
                record.progress = max(0, min(100, progress))
            if message is not None:
                record.message = message
            self._persist(task_id)

    def append_fail_reason(self, task_id: str, reason: str) -> None:
        with self._lock:
            record = self._tasks.get(task_id)
            if not record:
                return
            record.fail_reasons.append(reason)

    def complete_task(self, task_id: str, report: OptimizationReportData) -> None:
        with self._lock:
            record = self._tasks.get(task_id)
            if not record:
                return
            record.state = TaskState.COMPLETED
            record.progress = 100
            record.message = "completed"
            record.report = report
            record.completed_at = datetime.utcnow()
            self._persist(task_id)

    def fail_task(self, task_id: str, message: str) -> None:
        with self._lock:
            record = self._tasks.get(task_id)
            if not record:
                return
            record.state = TaskState.FAILED
            record.message = message
            record.progress = 100
            record.completed_at = datetime.utcnow()
            self._persist(task_id)

    def list_tasks_by_plan(self, plan_id: str) -> List[Tuple[str, TaskRecord]]:
        with self._lock:
            return [
                (task_id, task)
                for task_id, task in self._tasks.items()
                if task.plan_id == plan_id
            ]

    def delete_tasks_for_plan(self, plan_id: str) -> None:
        with self._lock:
            for task_id in list(self._tasks.keys()):
                if self._tasks[task_id].plan_id == plan_id:
                    self._tasks.pop(task_id)
                    self._db.delete_task(task_id)

    # ------------------------------------------------------------------ Helpers
    def _load_from_db(self) -> None:
        for row in self._db.load_tasks():
            request = OptimizationRequest(**row["request"])
            report_data = row["report"]
            report = (
                OptimizationReportData(**report_data)
                if report_data
                else None
            )
            record = TaskRecord(
                request=request,
                state=TaskState(row["state"]),
                progress=row["progress"],
                message=row.get("message"),
                fail_reasons=row.get("fail_reasons", []),
                report=report,
                plan_id=row.get("plan_id"),
                created_at=datetime.fromisoformat(row["created_at"]),
                completed_at=datetime.fromisoformat(row["completed_at"])
                if row.get("completed_at")
                else None,
            )
            self._tasks[row["task_id"]] = record

    def _persist(self, task_id: str) -> None:
        record = self._tasks.get(task_id)
        if not record:
            return
        task_payload = {
            "task_id": task_id,
            "plan_id": record.plan_id,
            "request": record.request.dict(),
            "state": record.state.value,
            "progress": record.progress,
            "message": record.message,
            "fail_reasons": record.fail_reasons,
            "report": record.report.dict() if record.report else None,
            "created_at": record.created_at.isoformat(),
            "completed_at": record.completed_at.isoformat() if record.completed_at else None,
        }
        self._db.save_task(task_payload)


store = OptimizerTaskStore()
