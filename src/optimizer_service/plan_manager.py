"""Optimization plan management and scheduling."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from threading import Lock
from typing import Any, Dict, List, Optional
import uuid
from time import monotonic

from loguru import logger
from .schemas import (
    OptimizationAlgorithm,
    OptimizationPlanCreate,
    OptimizationPlanResponse,
    OptimizationPlanUpdate,
    OptimizationRequest,
    TaskState,
    as_threshold_parameter_set,
)
from .storage import OptimizerTaskStore, TaskRecord, store as global_task_store
from .db import OptimizerDatabase, optimizer_db
from .tasks import run_optimization_task


def _model_dump(obj: Any, **kwargs: Any) -> Dict[str, Any]:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(**kwargs)
    return obj.dict(**kwargs)


def _serialize_parameter_set(value: Any) -> Dict[str, Any]:
    parameter_set = as_threshold_parameter_set(value)
    return _model_dump(parameter_set)


def _flatten_parameter_set(value: Any) -> Dict[str, List[Any]]:
    parameter_set = as_threshold_parameter_set(value)
    return parameter_set.flatten()


@dataclass
class OptimizationPlan:
    id: str
    name: str
    strategy: str
    symbol: str
    base_metrics: List[str]
    feature_metrics: List[str]
    parameters: Dict[str, Any]
    algorithm: OptimizationAlgorithm
    start_date: date
    end_date: date
    frequency_minutes: int
    iterations: Optional[int]
    created_at: datetime
    updated_at: datetime
    is_active: bool = False
    last_run_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = None
    task_history: List[str] = field(default_factory=list)
    metrics: List[str] = field(init=False)

    def __post_init__(self) -> None:
        self.refresh_metrics()

    def refresh_metrics(self) -> None:
        seen = set()
        combined: List[str] = []
        for value in list(self.base_metrics) + list(self.feature_metrics):
            if value in seen:
                continue
            seen.add(value)
            combined.append(value)
        self.metrics = combined

    def to_request(self) -> OptimizationRequest:
        return OptimizationRequest(
            strategy=self.strategy,
            start_date=self.start_date,
            end_date=self.end_date,
            parameters=_flatten_parameter_set(self.parameters),
            algorithm=self.algorithm,
            iterations=self.iterations,
            plan_id=self.id,
        )


class PlanManager:
    def __init__(self, task_store: OptimizerTaskStore, database: OptimizerDatabase = optimizer_db) -> None:
        self._task_store = task_store
        self._db = database
        self._plans: Dict[str, OptimizationPlan] = {}
        self._lock = Lock()
        self._scheduled_tasks: Dict[str, asyncio.Task] = {}
        self._resumed_tasks: set[str] = set()
        if self._task_store is None:
            raise ValueError("PlanManager requires a task store instance")
        self._load_existing()

    # CRUD -----------------------------------------------------------------
    def create_plan(self, payload: OptimizationPlanCreate) -> OptimizationPlan:
        now = datetime.utcnow()
        plan_id = str(uuid.uuid4())
        parameter_dict = _serialize_parameter_set(payload.parameters)
        plan = OptimizationPlan(
            id=plan_id,
            name=payload.name,
            strategy=payload.strategy,
            symbol=payload.symbol,
            base_metrics=list(payload.base_metrics),
            feature_metrics=list(payload.feature_metrics),
            parameters=parameter_dict,
            algorithm=payload.algorithm,
            start_date=payload.start_date,
            end_date=payload.end_date,
            frequency_minutes=payload.frequency_minutes,
            iterations=payload.iterations,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._plans[plan_id] = plan
        self._db.save_plan(self._plan_to_dict(plan))
        return plan

    def list_plans(self) -> List[OptimizationPlan]:
        with self._lock:
            return list(self._plans.values())

    def get_plan(self, plan_id: str) -> OptimizationPlan:
        with self._lock:
            if plan_id not in self._plans:
                raise KeyError(plan_id)
            return self._plans[plan_id]

    def update_plan(self, plan_id: str, payload: OptimizationPlanUpdate) -> OptimizationPlan:
        with self._lock:
            if plan_id not in self._plans:
                raise KeyError(plan_id)
            plan = self._plans[plan_id]
            if plan.is_active:
                raise RuntimeError("active_plan_not_editable")
            data = _model_dump(payload, exclude_unset=True)
            for field_name, value in data.items():
                if field_name == "parameters" and value is not None:
                    value = _serialize_parameter_set(value)
                elif field_name in {"base_metrics", "feature_metrics"} and value is not None:
                    value = list(value)
                if hasattr(plan, field_name) and value is not None:
                    setattr(plan, field_name, value)
            plan.updated_at = datetime.utcnow()
            if data.keys() & {"base_metrics", "feature_metrics"}:
                plan.refresh_metrics()
            self._db.save_plan(self._plan_to_dict(plan))
            return plan

    def delete_plan(self, plan_id: str) -> None:
        plan = self.get_plan(plan_id)
        self.stop_plan(plan_id)
        self._task_store.delete_tasks_for_plan(plan_id)
        self._db.delete_plan(plan_id)
        with self._lock:
            self._plans.pop(plan_id, None)

    # Scheduling ------------------------------------------------------------
    async def start_plan(self, plan_id: str) -> OptimizationPlan:
        plan = self.get_plan(plan_id)
        if plan.is_active:
            return plan
        plan.is_active = True
        plan.updated_at = datetime.utcnow()
        await self._schedule_next(plan, delay=0)
        self._db.save_plan(self._plan_to_dict(plan))
        return plan

    def stop_plan(self, plan_id: str) -> OptimizationPlan:
        plan = self.get_plan(plan_id)
        plan.is_active = False
        plan.next_run_at = None
        plan.updated_at = datetime.utcnow()
        task = self._scheduled_tasks.pop(plan_id, None)
        if task:
            task.cancel()
        for task_id, record in self._task_store.list_tasks_by_plan(plan_id):
            if record.state in {TaskState.RUNNING, TaskState.PENDING}:
                self._task_store.request_stop(task_id)
        self._db.save_plan(self._plan_to_dict(plan))
        return plan

    async def run_plan_once(self, plan_id: str, *, terminate_existing: bool = False) -> str:
        plan = self.get_plan(plan_id)
        active = self._task_store.find_active_plan_task(plan.id)
        if active:
            if not terminate_existing:
                raise RuntimeError("plan_has_active_task")
            await self._wait_for_task_stop(active[0])
            # After waiting ensure no other active task remains.
            follow_up_active = self._task_store.find_active_plan_task(plan.id)
            if follow_up_active:
                raise RuntimeError("plan_has_active_task")
        existing = self._scheduled_tasks.pop(plan.id, None)
        if existing:
            existing.cancel()
        follow_up = plan.is_active and plan.frequency_minutes > 0
        return await self._trigger_plan(plan, schedule_follow_up=follow_up, origin="manual")

    async def _schedule_next(self, plan: OptimizationPlan, delay: float) -> None:
        async def runner() -> None:
            try:
                if delay > 0:
                    await asyncio.sleep(delay)
                if not plan.is_active:
                    return
                await self._trigger_plan(plan, schedule_follow_up=True, origin="auto")
            except asyncio.CancelledError:  # pragma: no cover
                return

        loop = asyncio.get_running_loop()
        existing = self._scheduled_tasks.get(plan.id)
        if existing:
            existing.cancel()
        plan.next_run_at = datetime.utcnow() + timedelta(seconds=delay)
        task = loop.create_task(runner())
        self._scheduled_tasks[plan.id] = task

    async def _trigger_plan(self, plan: OptimizationPlan, schedule_follow_up: bool, *, origin: str) -> str:
        request = plan.to_request()
        active = self._task_store.find_active_plan_task(plan.id)
        if active:
            if origin == "manual":
                raise RuntimeError("plan_has_active_task")
            logger.info(
                "Plan %s has an active task %s, skipping automatic trigger",
                plan.id,
                active[0],
            )
            if schedule_follow_up and plan.is_active:
                retry_delay = max(5, min(plan.frequency_minutes * 60, 60))
                await self._schedule_next(plan, delay=retry_delay)
            self._db.save_plan(self._plan_to_dict(plan))
            return active[0]
        if origin != "manual" and self._task_store.has_running_tasks(exclude_plan_id=plan.id):
            logger.info(
                "Detected other running tasks while triggering plan %s automatically; postponing",
                plan.id,
            )
            if schedule_follow_up and plan.is_active:
                retry_delay = max(5, min(plan.frequency_minutes * 60, 60))
                await self._schedule_next(plan, delay=retry_delay)
            self._db.save_plan(self._plan_to_dict(plan))
            if plan.task_history:
                return plan.task_history[-1]
            return plan.id
        task_id = str(uuid.uuid4())
        self._task_store.create_task(
            task_id,
            request,
            plan_id=plan.id,
            initial_state=TaskState.RUNNING,
            progress=0,
            message="starting",
        )
        plan.task_history.append(task_id)
        plan.updated_at = datetime.utcnow()
        await asyncio.to_thread(run_optimization_task, task_id, request, self._task_store)
        plan.last_run_at = datetime.utcnow()
        if schedule_follow_up and plan.is_active and plan.frequency_minutes > 0:
            delay = max(1, plan.frequency_minutes * 60)
            await self._schedule_next(plan, delay=delay)
        else:
            plan.next_run_at = None
            self._scheduled_tasks.pop(plan.id, None)
        self._db.save_plan(self._plan_to_dict(plan))
        return task_id

    # Reports ---------------------------------------------------------------
    def list_plan_reports(self, plan_id: str) -> List[tuple[str, TaskRecord]]:
        tasks = self._task_store.list_tasks_by_plan(plan_id)
        return sorted(tasks, key=lambda pair: pair[1].created_at, reverse=True)

    # Serialization ---------------------------------------------------------
    def to_response(self, plan: OptimizationPlan) -> OptimizationPlanResponse:
        return OptimizationPlanResponse(
            id=plan.id,
            name=plan.name,
            strategy=plan.strategy,
            symbol=plan.symbol,
            base_metrics=list(plan.base_metrics),
            feature_metrics=list(plan.feature_metrics),
            metrics=list(plan.metrics),
            parameters=plan.parameters,
            algorithm=plan.algorithm,
            start_date=plan.start_date,
            end_date=plan.end_date,
            frequency_minutes=plan.frequency_minutes,
            iterations=plan.iterations,
            created_at=plan.created_at,
            updated_at=plan.updated_at,
            is_active=plan.is_active,
            last_run_at=plan.last_run_at,
            next_run_at=plan.next_run_at,
            task_history=list(plan.task_history),
        )


    def _plan_to_dict(self, plan: OptimizationPlan) -> Dict[str, Any]:
        return {
            "id": plan.id,
            "name": plan.name,
            "strategy": plan.strategy,
            "symbol": plan.symbol,
            "metrics": {
                "all": list(plan.metrics),
                "base": list(plan.base_metrics),
                "feature": list(plan.feature_metrics),
            },
            "parameters": _serialize_parameter_set(plan.parameters),
            "algorithm": plan.algorithm.value,
            "start_date": plan.start_date.isoformat(),
            "end_date": plan.end_date.isoformat(),
            "frequency_minutes": plan.frequency_minutes,
            "iterations": plan.iterations,
            "is_active": plan.is_active,
            "created_at": plan.created_at.isoformat(),
            "updated_at": plan.updated_at.isoformat(),
            "last_run_at": plan.last_run_at.isoformat() if plan.last_run_at else None,
            "next_run_at": plan.next_run_at.isoformat() if plan.next_run_at else None,
            "task_history": list(plan.task_history),
        }

    def _load_existing(self) -> None:
        for plan_data in self._db.load_plans():
            plan = OptimizationPlan(
                id=plan_data["id"],
                name=plan_data["name"],
                strategy=plan_data["strategy"],
                symbol=plan_data["symbol"],
                base_metrics=plan_data.get("base_metrics", []) or plan_data.get("metrics", []),
                feature_metrics=plan_data.get("feature_metrics", []),
                parameters=_serialize_parameter_set(plan_data["parameters"]),
                algorithm=OptimizationAlgorithm(plan_data["algorithm"]),
                start_date=date.fromisoformat(plan_data["start_date"]),
                end_date=date.fromisoformat(plan_data["end_date"]),
                frequency_minutes=plan_data["frequency_minutes"],
                iterations=plan_data["iterations"],
                created_at=datetime.fromisoformat(plan_data["created_at"]),
                updated_at=datetime.fromisoformat(plan_data["updated_at"]),
                is_active=plan_data["is_active"],
                last_run_at=datetime.fromisoformat(plan_data["last_run_at"])
                if plan_data.get("last_run_at")
                else None,
                next_run_at=datetime.fromisoformat(plan_data["next_run_at"])
                if plan_data.get("next_run_at")
                else None,
                task_history=plan_data.get("task_history", []),
            )
            self._plans[plan.id] = plan

    async def resume_active_plans(self) -> None:
        await self._resume_interrupted_tasks()
        now = datetime.utcnow()
        for plan in self.list_plans():
            if not plan.is_active:
                continue
            delay = 0.0
            if plan.next_run_at and plan.next_run_at > now:
                delay = (plan.next_run_at - now).total_seconds()
            await self._schedule_next(plan, delay=delay)

    async def _resume_interrupted_tasks(self) -> None:
        pending_tasks = self._task_store.list_incomplete_tasks()
        for task_id, record in pending_tasks:
            if task_id in self._resumed_tasks:
                continue
            logger.info("Resuming optimizer task %s", task_id)
            self._resumed_tasks.add(task_id)
            self._task_store.mark_resumed(task_id)
            self._task_store.update_state(
                task_id,
                TaskState.RUNNING,
                progress=record.progress,
                message="resuming",
            )
            asyncio.create_task(
                asyncio.to_thread(run_optimization_task, task_id, record.request, self._task_store)
            )

    async def _wait_for_task_stop(self, task_id: str, timeout: float = 30.0) -> None:
        accepted = self._task_store.request_stop(task_id)
        if not accepted:
            return
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            record = self._task_store.get_task(task_id)
            if not record or record.state in {
                TaskState.COMPLETED,
                TaskState.FAILED,
                TaskState.STOPPED,
            }:
                return
            await asyncio.sleep(0.5)
        logger.warning("Task %s did not stop within timeout, forcing state to stopped", task_id)
        self._task_store.mark_stopped(task_id)


plan_manager = PlanManager(task_store=global_task_store)
