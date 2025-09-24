"""API routes for the optimizer module."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, BackgroundTasks, HTTPException, Response, status, Query
from fastapi.responses import JSONResponse

from src.database import Strategy as DatabaseStrategy, get_db_manager

from .schemas import (
    ErrorResponse,
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
    TaskState,
)
from .plan_manager import plan_manager
from .metadata import (
    get_available_algorithms,
    get_available_base_metrics,
    get_available_feature_metrics,
    get_symbol_data_range,
)
from .storage import store
from .strategy_generator import StrategyGenerationError, generate_strategy_file
from .tasks import run_optimization_task

router = APIRouter(prefix="/api/optimize", tags=["optimizer"])


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
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
)
async def run_plan(plan_id: str) -> OptimizationPlanRunResponse:
    try:
        task_id = await plan_manager.run_plan_once(plan_id)
        plan = plan_manager.get_plan(plan_id)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
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
    reports_payload = [
        OptimizationPlanReportEntry(
            task_id=task_id,
            state=record.state,
            created_at=record.created_at,
            completed_at=record.completed_at,
            message=record.message,
            report=record.report,
            runtime=record.runtime,
        )
        for task_id, record in records
    ]
    return OptimizationPlanReportList(plan_id=plan_id, reports=reports_payload)


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
