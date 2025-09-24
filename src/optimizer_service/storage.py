"""In-memory storage for optimizer tasks."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import re
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

from .engine import OptimizationEngine
from .reporting import build_conclusion, compute_return_distribution, compute_runtime_summary
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
    runtime: Optional[Dict[str, Any]] = None


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
            tasks = []
            for task_id, task in self._tasks.items():
                if task.plan_id != plan_id:
                    continue
                self._ensure_report_summary(task_id, task)
                self._ensure_runtime_snapshot(task)
                tasks.append((task_id, task))
            return tasks

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

    def _ensure_report_summary(self, task_id: str, record: TaskRecord) -> None:
        report = record.report
        if not report or not report.summary:
            return

        summary = dict(report.summary)
        changed = False

        tested = summary.get("tested_combinations")
        if tested is None:
            tested = len(report.results) + len(report.fail_reasons or [])
            summary["tested_combinations"] = tested
            changed = True

        success = summary.get("successful_combinations")
        if success is None:
            success = len(report.results)
            summary["successful_combinations"] = success
            changed = True

        failed = summary.get("failed_combinations")
        if failed is None:
            failed = len(report.fail_reasons or [])
            summary["failed_combinations"] = failed
            changed = True

        if "best_sharpe" not in summary and report.results:
            best_sharpe = report.results[0].metrics.get("sharpe")
            if best_sharpe is not None:
                summary["best_sharpe"] = round(best_sharpe, 3)
                changed = True

        if "best_max_drawdown" not in summary and report.results:
            max_drawdown = report.results[0].metrics.get("max_drawdown")
            if max_drawdown is not None:
                summary["best_max_drawdown"] = round(max_drawdown, 4)
                changed = True

        if "best_annual_return" not in summary and report.results:
            annual_return = report.results[0].metrics.get("annual_return")
            if annual_return is not None:
                summary["best_annual_return"] = round(annual_return, 4)
                changed = True

        if "annual_return_distribution" not in summary:
            distribution = compute_return_distribution(report.results)
            summary["annual_return_distribution"] = distribution
            changed = True
        else:
            distribution = summary.get("annual_return_distribution") or {}

        if "runtime" not in summary and record.completed_at:
            actual_total = tested or (len(report.results) + len(report.fail_reasons or []))
            estimated_total = actual_total
            try:
                estimated_total = OptimizationEngine(record.request).estimated_total()
            except Exception:  # pragma: no cover - defensive
                estimated_total = actual_total
            started_at = record.created_at
            completed_at = record.completed_at
            elapsed = 0.0
            if started_at and completed_at:
                elapsed = max((completed_at - started_at).total_seconds(), 0.0)
            runtime_summary = compute_runtime_summary(
                started_at or datetime.utcnow(),
                completed_at,
                elapsed,
                estimated_total,
                actual_total,
            )
            runtime_summary["status"] = record.state.value
            summary["runtime"] = runtime_summary
            changed = True

        if "conclusion" not in summary:
            summary["conclusion"] = build_conclusion(
                tested or 0,
                success or 0,
                failed or 0,
                summary.get("best_sharpe"),
                summary.get("best_max_drawdown"),
                distribution,
            )
            changed = True

        if changed:
            report.summary = summary
            self._persist(task_id)

    def _ensure_runtime_snapshot(self, record: TaskRecord) -> None:
        runtime: Optional[Dict[str, Any]] = None
        summary = record.report.summary if record.report else None
        if summary:
            runtime_data = summary.get("runtime")
            if isinstance(runtime_data, dict):
                runtime = dict(runtime_data)
                runtime.setdefault("status", record.state.value)
        if runtime is None:
            runtime = self._compute_active_runtime(record)
        record.runtime = runtime

    def _compute_active_runtime(self, record: TaskRecord) -> Optional[Dict[str, Any]]:
        if record.state not in {TaskState.RUNNING, TaskState.FAILED}:
            return None
        try:
            estimated_total = OptimizationEngine(record.request).estimated_total()
        except Exception:  # pragma: no cover - defensive
            estimated_total = 0
        processed = self._extract_processed_count(record.message)
        if not processed and estimated_total and record.progress:
            processed = max(int(estimated_total * record.progress / 100), 0)
        if record.state == TaskState.FAILED and processed == 0:
            processed = estimated_total
        now = datetime.utcnow()
        elapsed = max((now - record.created_at).total_seconds(), 0.0)
        runtime = compute_runtime_summary(
            record.created_at,
            now,
            elapsed,
            estimated_total,
            processed,
        )
        runtime["status"] = record.state.value
        runtime["actual_total_combinations"] = processed
        return runtime

    @staticmethod
    def _extract_processed_count(message: Optional[str]) -> int:
        if not message:
            return 0
        match = re.search(r"processed\s+(\d+)\/(\d+)", message)
        if not match:
            return 0
        try:
            return int(match.group(1))
        except ValueError:  # pragma: no cover - defensive
            return 0


store = OptimizerTaskStore()
