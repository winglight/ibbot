"""Pydantic schemas and enums for optimizer service."""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Literal

try:
    from pydantic import BaseModel, Field, field_validator, FieldValidationInfo
    PYDANTIC_V2 = True
except ImportError:  # pragma: no cover - fallback for pydantic v1
    from pydantic import BaseModel, Field, validator

    PYDANTIC_V2 = False


class OptimizationAlgorithm(str, Enum):
    GRID = "grid"
    RANDOM = "random"
    BAYESIAN = "bayesian"


class TaskState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class OptimizationRequest(BaseModel):
    strategy: str = Field(..., min_length=1, description="Strategy identifier to optimize")
    start_date: date = Field(..., description="Backtest start date")
    end_date: date = Field(..., description="Backtest end date")
    parameters: Dict[str, List[Any]] = Field(..., description="Parameter grid or ranges")
    algorithm: OptimizationAlgorithm = Field(..., description="Search algorithm to use")
    iterations: Optional[int] = Field(
        None,
        ge=1,
        description="Maximum iterations for iterative algorithms (e.g. random, bayesian)",
    )
    plan_id: Optional[str] = Field(None, description="Optional plan identifier tying results to a plan")

    if PYDANTIC_V2:  # pragma: no branch

        @field_validator("end_date")
        def validate_dates(cls, v: date, info: FieldValidationInfo) -> date:
            start = info.data.get("start_date")
            if start and v < start:
                raise ValueError("end_date must not be earlier than start_date")
            return v

        @field_validator("parameters")
        def validate_parameters(
            cls, param_map: Dict[str, List[Any]]
        ) -> Dict[str, List[Any]]:
            if not param_map:
                raise ValueError("parameters must include at least one entry")
            for name, values in param_map.items():
                if not values:
                    raise ValueError(f"parameter '{name}' must define at least one candidate value")
            return param_map

        @field_validator("iterations")
        def validate_iterations(
            cls, iterations: Optional[int], info: FieldValidationInfo
        ) -> Optional[int]:
            if iterations is None:
                return iterations
            algorithm: Optional[OptimizationAlgorithm] = info.data.get("algorithm")
            if algorithm == OptimizationAlgorithm.GRID and iterations is not None:
                raise ValueError("iterations is not supported for grid search")
            return iterations

    else:

        @validator("end_date")  # type: ignore[misc]
        def validate_dates(cls, v: date, values: Dict[str, Any]) -> date:
            start = values.get("start_date")
            if start and v < start:
                raise ValueError("end_date must not be earlier than start_date")
            return v

        @validator("parameters")  # type: ignore[misc]
        def validate_parameters(cls, param_map: Dict[str, List[Any]]) -> Dict[str, List[Any]]:
            if not param_map:
                raise ValueError("parameters must include at least one entry")
            for name, values in param_map.items():
                if not values:
                    raise ValueError(f"parameter '{name}' must define at least one candidate value")
            return param_map

        @validator("iterations")  # type: ignore[misc]
        def validate_iterations(
            cls, iterations: Optional[int], values: Dict[str, Any]
        ) -> Optional[int]:
            if iterations is None:
                return iterations
            algorithm: Optional[OptimizationAlgorithm] = values.get("algorithm")
            if algorithm == OptimizationAlgorithm.GRID and iterations is not None:
                raise ValueError("iterations is not supported for grid search")
            return iterations


class OptimizationStartResponse(BaseModel):
    status: str = Field(..., example="ok")
    task_id: str = Field(..., description="Identifier of the optimization task")


class OptimizationTaskStatusResponse(BaseModel):
    status: str = Field(..., description="Request processing status (ok or error)")
    task_id: str
    state: TaskState
    progress: int = Field(..., ge=0, le=100)
    message: Optional[str] = None


class OptimizationResultItem(BaseModel):
    parameters: Dict[str, Any]
    metrics: Dict[str, float]


class OptimizationReportData(BaseModel):
    task_id: str
    results: List[OptimizationResultItem]
    summary: Dict[str, Any]
    equity_curve: List[Dict[str, Any]]
    fail_reasons: List[str] = Field(default_factory=list)


class OptimizationReportResponse(BaseModel):
    status: str
    task_id: str
    report: Optional[OptimizationReportData] = None
    message: Optional[str] = None


class ErrorResponse(BaseModel):
    status: Literal["error"] = "error"
    message: str


class OptimizationPlanBase(BaseModel):
    name: str = Field(..., min_length=1)
    strategy: str = Field(..., min_length=1, description="Strategy identifier associated with the plan")
    symbol: str = Field(..., min_length=1, description="Instrument or market symbol")
    metrics: List[str] = Field(..., description="Metrics to evaluate, e.g. obi/ofi/trend")
    parameters: Dict[str, List[Any]]
    algorithm: OptimizationAlgorithm
    start_date: date
    end_date: date
    frequency_minutes: int = Field(..., ge=0, description="Interval in minutes for generating reports")
    iterations: Optional[int] = Field(None, ge=1)

    if PYDANTIC_V2:  # pragma: no branch

        @field_validator("metrics")
        def validate_metrics(cls, v: List[str]) -> List[str]:
            if not v:
                raise ValueError("metrics must not be empty")
            return [value.strip() for value in v if value.strip()]

        @field_validator("end_date")
        def validate_plan_dates(cls, v: date, info: FieldValidationInfo) -> date:
            start = info.data.get("start_date")
            if start and v < start:
                raise ValueError("end_date must not be earlier than start_date")
            return v

    else:

        @validator("metrics")  # type: ignore[misc]
        def validate_metrics(cls, v: List[str]) -> List[str]:
            if not v:
                raise ValueError("metrics must not be empty")
            return [value.strip() for value in v if value.strip()]

        @validator("end_date")  # type: ignore[misc]
        def validate_plan_dates(cls, v: date, values: Dict[str, Any]) -> date:
            start = values.get("start_date")
            if start and v < start:
                raise ValueError("end_date must not be earlier than start_date")
            return v


class OptimizationPlanCreate(OptimizationPlanBase):
    pass


class OptimizationPlanUpdate(BaseModel):
    name: Optional[str]
    strategy: Optional[str]
    symbol: Optional[str]
    metrics: Optional[List[str]]
    parameters: Optional[Dict[str, List[Any]]]
    algorithm: Optional[OptimizationAlgorithm]
    start_date: Optional[date]
    end_date: Optional[date]
    frequency_minutes: Optional[int]
    iterations: Optional[int]

    if PYDANTIC_V2:  # pragma: no branch

        @field_validator("metrics")
        def validate_metrics(cls, v: Optional[List[str]]):
            if v is None:
                return v
            # For update operations, allow empty metrics list
            if not v:
                return v
            return [value.strip() for value in v if value.strip()]

        @field_validator("end_date")
        def validate_plan_dates(cls, v: Optional[date], info: FieldValidationInfo):
            start = info.data.get("start_date")
            if v and start and v < start:
                raise ValueError("end_date must not be earlier than start_date")
            return v

    else:

        @validator("metrics")  # type: ignore[misc]
        def validate_metrics(cls, v: Optional[List[str]]):
            if v is None:
                return v
            # For update operations, allow empty metrics list
            if not v:
                return v
            return [value.strip() for value in v if value.strip()]

        @validator("end_date")  # type: ignore[misc]
        def validate_plan_dates(cls, v: Optional[date], values: Dict[str, Any]):
            start = values.get("start_date")
            if v and start and v < start:
                raise ValueError("end_date must not be earlier than start_date")
            return v


class OptimizationPlanResponse(OptimizationPlanBase):
    id: str
    created_at: datetime
    updated_at: datetime
    is_active: bool
    last_run_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = None
    task_history: List[str] = Field(default_factory=list)


class OptimizationPlanListResponse(BaseModel):
    plans: List[OptimizationPlanResponse]


class OptimizationPlanActionResponse(BaseModel):
    status: Literal["ok"] = "ok"
    plan: OptimizationPlanResponse


class OptimizationPlanRunResponse(BaseModel):
    status: Literal["ok"] = "ok"
    task_id: str
    plan: OptimizationPlanResponse


class OptimizationPlanReportEntry(BaseModel):
    task_id: str
    state: TaskState
    created_at: datetime
    completed_at: Optional[datetime]
    message: Optional[str]
    report: Optional[OptimizationReportData]


class OptimizationPlanReportList(BaseModel):
    plan_id: str
    reports: List[OptimizationPlanReportEntry]


class OptimizerMetadataResponse(BaseModel):
    metrics: List[str]
    algorithms: List[str]


class OptimizerDataRangeResponse(BaseModel):
    symbol: str
    start_date: date
    end_date: date
    records: Optional[int]
