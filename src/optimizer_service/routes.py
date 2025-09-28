"""API routes for the optimizer module."""

from __future__ import annotations

import uuid

from typing import Any, Dict, List, Optional, Set

from fastapi import APIRouter, BackgroundTasks, HTTPException, Response, status, Query
from fastapi.responses import JSONResponse

from src.database import Strategy as DatabaseStrategy, get_db_manager

from .schemas import (
    ErrorResponse,
    OptimizationReportData,
    OptimizationReportResponse,
    OptimizationRequest,
    OptimizationStartResponse,
    OptimizationTaskStatusResponse,
    OptimizationPlanActionResponse,
    OptimizationPlanCreate,
    OptimizationPlanListResponse,
    OptimizationPlanReportList,
    OptimizationPlanReportEntry,
    OptimizationPlanResponse,
    OptimizationPlanRunResponse,
    OptimizationPlanUpdate,
    StrategyGenerationRequest,
    StrategyGenerationResponse,
    OptimizerDataRangeResponse,
    OptimizerMetadataResponse,
    ParameterGroupPerformanceResponse,
    TaskState,
    TaskStopResponse,
)
from .plan_manager import plan_manager
from .metadata import (
    get_available_algorithms,
    get_available_base_metrics,
    get_available_feature_metrics,
    get_symbol_data_range,
)
from .storage import store, TaskRecord
from .analytics import summarize_parameter_groups
from .strategy_generator import StrategyGenerationError, generate_strategy_file
from .tasks import run_optimization_task
from .reporting import compute_return_histogram

router = APIRouter(prefix="/api/optimize", tags=["optimizer"])


def _normalize_fail_reason(reason: str) -> str:
    text = (reason or "").strip()
    if not text:
        return ""
    if ":" in text:
        prefix, suffix = text.split(":", 1)
        candidate = suffix.strip()
        text = candidate or prefix.strip()
    return text


def _coerce_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _unique_fail_reasons(record: TaskRecord, report: OptimizationReportData) -> List[str]:
    seen: Set[str] = set()
    aggregated: List[str] = []
    for source in (record.fail_reasons or []):
        normalized = _normalize_fail_reason(source)
        if normalized and normalized not in seen:
            seen.add(normalized)
            aggregated.append(normalized)
    if aggregated:
        return aggregated
    for source in report.fail_reasons or []:
        normalized = _normalize_fail_reason(source)
        if normalized and normalized not in seen:
            seen.add(normalized)
            aggregated.append(normalized)
    return aggregated


def _build_report_payload(record: TaskRecord) -> Optional[OptimizationReportData]:
    report = record.report
    if not report:
        return None

    summary: Dict[str, Any] = dict(report.summary or {})
    successful = _coerce_int(summary.get("successful_combinations"), len(report.results))
    summary["successful_combinations"] = successful
    failed = _coerce_int(summary.get("failed_combinations"), len(record.fail_reasons or []))
    summary["failed_combinations"] = failed
    if "tested_combinations" not in summary:
        summary["tested_combinations"] = successful + failed

    detailed_results = report.results[:3]
    summary["detailed_result_limit"] = 3
    summary["other_combination_count"] = max(successful - len(detailed_results), 0)

    if "annual_return_histogram" not in summary:
        summary["annual_return_histogram"] = compute_return_histogram(report.results)

    fail_reasons = _unique_fail_reasons(record, report)

    return OptimizationReportData(
        task_id=report.task_id,
        results=detailed_results,
        summary=summary,
        equity_curve=report.equity_curve,
        fail_reasons=fail_reasons,
    )


@router.on_event("startup")
async def resume_plans():  # pragma: no cover - startup hook
    await plan_manager.resume_active_plans()


@router.get("/metadata", response_model=OptimizerMetadataResponse)
async def get_optimizer_metadata() -> OptimizerMetadataResponse:
    return OptimizerMetadataResponse(
        base_metrics=get_available_base_metrics(),
        feature_metrics=get_available_feature_metrics(),
        algorithms=get_available_algorithms(),
    )


@router.get(
    "/data-range",
    response_model=OptimizerDataRangeResponse,
    responses={status.HTTP_400_BAD_REQUEST: {"model": ErrorResponse}},
)
async def get_data_range(symbol: str = Query(..., min_length=1)) -> OptimizerDataRangeResponse:
    info = get_symbol_data_range(symbol)
    return OptimizerDataRangeResponse(**info)


@router.post(
    "/start",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=OptimizationStartResponse,
    responses={status.HTTP_400_BAD_REQUEST: {"model": ErrorResponse}},
)
async def start_optimization(request: OptimizationRequest, background_tasks: BackgroundTasks) -> OptimizationStartResponse:
    task_id = str(uuid.uuid4())
    store.create_task(task_id, request, plan_id=request.plan_id)
    background_tasks.add_task(run_optimization_task, task_id, request, store)
    return OptimizationStartResponse(status="ok", task_id=task_id)


@router.get(
    "/status/{task_id}",
    response_model=OptimizationTaskStatusResponse,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
)
async def get_status(task_id: str) -> OptimizationTaskStatusResponse:
    record = store.get_task(task_id)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")
    return OptimizationTaskStatusResponse(
        status="ok",
        task_id=task_id,
        state=record.state,
        progress=record.progress,
        message=record.message,
    )


@router.get(
    "/report/{task_id}",
    response_model=OptimizationReportResponse,
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
        status.HTTP_202_ACCEPTED: {"model": OptimizationReportResponse},
    },
)
async def get_report(task_id: str):
    record = store.get_task(task_id)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")
    if record.state in {TaskState.PENDING, TaskState.RUNNING}:
        payload = OptimizationReportResponse(
            status="processing",
            task_id=task_id,
            message=record.message,
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=payload.dict(),
        )
    if record.state == TaskState.FAILED or not record.report:
        return OptimizationReportResponse(status="error", task_id=task_id, message=record.message)
    return OptimizationReportResponse(status="ok", task_id=task_id, report=record.report)


@router.post(
    "/tasks/{task_id}/stop",
    response_model=TaskStopResponse,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
)
async def stop_task(task_id: str) -> TaskStopResponse:
    record = store.get_task(task_id)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")
    if record.state in {TaskState.COMPLETED, TaskState.FAILED, TaskState.STOPPED}:
        return TaskStopResponse(task_id=task_id, accepted=False)
    accepted = store.request_stop(task_id)
    return TaskStopResponse(task_id=task_id, accepted=accepted)


# Plan management -------------------------------------------------------------


@router.post(
    "/plans",
    status_code=status.HTTP_201_CREATED,
    response_model=OptimizationPlanResponse,
)
async def create_plan(payload: OptimizationPlanCreate) -> OptimizationPlanResponse:
    plan = plan_manager.create_plan(payload)
    return plan_manager.to_response(plan)


@router.get("/plans", response_model=OptimizationPlanListResponse)
async def list_plans() -> OptimizationPlanListResponse:
    plans = [plan_manager.to_response(plan) for plan in plan_manager.list_plans()]
    return OptimizationPlanListResponse(plans=plans)


@router.get(
    "/plans/{plan_id}",
    response_model=OptimizationPlanResponse,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
)
async def get_plan(plan_id: str) -> OptimizationPlanResponse:
    try:
        plan = plan_manager.get_plan(plan_id)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
    return plan_manager.to_response(plan)


@router.patch(
    "/plans/{plan_id}",
    response_model=OptimizationPlanResponse,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
)
async def update_plan(plan_id: str, payload: OptimizationPlanUpdate) -> OptimizationPlanResponse:
    try:
        plan = plan_manager.update_plan(plan_id, payload)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
    except RuntimeError as exc:
        if str(exc) == "active_plan_not_editable":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="plan must be paused before editing")
        raise
    return plan_manager.to_response(plan)


@router.delete(
    "/plans/{plan_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
)
async def delete_plan(plan_id: str) -> Response:
    try:
        plan_manager.delete_plan(plan_id)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/plans/{plan_id}/start",
    response_model=OptimizationPlanActionResponse,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
)
async def start_plan(plan_id: str) -> OptimizationPlanActionResponse:
    try:
        plan = await plan_manager.start_plan(plan_id)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
    return OptimizationPlanActionResponse(plan=plan_manager.to_response(plan))


@router.post(
    "/plans/{plan_id}/stop",
    response_model=OptimizationPlanActionResponse,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
)
async def stop_plan(plan_id: str) -> OptimizationPlanActionResponse:
    try:
        plan = plan_manager.stop_plan(plan_id)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
    return OptimizationPlanActionResponse(plan=plan_manager.to_response(plan))


@router.post(
    "/plans/{plan_id}/run",
    response_model=OptimizationPlanRunResponse,
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
        status.HTTP_409_CONFLICT: {"model": ErrorResponse},
    },
)
async def run_plan(
    plan_id: str,
    terminate_existing: bool = Query(
        False,
        description="Whether to stop any running task for the plan before starting a new one",
    ),
) -> OptimizationPlanRunResponse:
    try:
        task_id = await plan_manager.run_plan_once(plan_id, terminate_existing=terminate_existing)
        plan = plan_manager.get_plan(plan_id)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
    except RuntimeError as exc:
        if str(exc) == "plan_has_active_task":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="plan has an active task",
            )
        raise
    return OptimizationPlanRunResponse(task_id=task_id, plan=plan_manager.to_response(plan))


@router.get(
    "/plans/{plan_id}/reports",
    response_model=OptimizationPlanReportList,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
)
async def list_plan_reports(plan_id: str) -> OptimizationPlanReportList:
    try:
        plan_manager.get_plan(plan_id)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
    records = plan_manager.list_plan_reports(plan_id)
    reports_payload = []
    for task_id, record in records:
        condensed_report = _build_report_payload(record)
        reports_payload.append(
            OptimizationPlanReportEntry(
                task_id=task_id,
                state=record.state,
                created_at=record.created_at,
                completed_at=record.completed_at,
                message=record.message,
                report=condensed_report,
                runtime=record.runtime,
            )
        )
    return OptimizationPlanReportList(plan_id=plan_id, reports=reports_payload)


@router.get(
    "/parameter-groups",
    response_model=ParameterGroupPerformanceResponse,
)
async def list_parameter_group_performance() -> ParameterGroupPerformanceResponse:
    return summarize_parameter_groups(plan_manager, store)


@router.post(
    "/strategies",
    status_code=status.HTTP_201_CREATED,
    response_model=StrategyGenerationResponse,
    responses={status.HTTP_400_BAD_REQUEST: {"model": ErrorResponse}},
)
async def generate_strategy(payload: StrategyGenerationRequest) -> StrategyGenerationResponse:
    try:
        file_path, class_name = generate_strategy_file(
            strategy_name=payload.strategy_name,
            base_parameters=payload.base_parameters,
            feature_parameters=payload.feature_parameters,
            plan_id=payload.plan_id,
            task_id=payload.task_id,
        )
    except StrategyGenerationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    normalized_path = file_path.replace("\\", "/")
    description_parts = ["DOM优化器自动生成策略"]
    metadata = []
    if payload.plan_id:
        metadata.append(f"计划: {payload.plan_id}")
    if payload.task_id:
        metadata.append(f"任务: {payload.task_id}")
    if metadata:
        description_parts.append(f"（{'，'.join(metadata)}）")
    description = "".join(description_parts)

    db_manager = get_db_manager()
    db_record = DatabaseStrategy(
        name=payload.strategy_name,
        strategy_type="internal",
        enabled=True,
        api_endpoint=None,
        file_path=normalized_path,
        description=description,
    )
    try:
        db_manager.save_strategy(db_record)
    except Exception as exc:  # pragma: no cover - safety net
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to persist strategy: {exc}",
        )

    return StrategyGenerationResponse(file_path=normalized_path, class_name=class_name)
