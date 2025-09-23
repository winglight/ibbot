"""Pydantic schemas and enums for optimizer service."""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Literal

try:
    from pydantic import BaseModel, Field, field_validator, FieldValidationInfo, model_validator
    PYDANTIC_V2 = True
except ImportError:  # pragma: no cover - fallback for pydantic v1
    from pydantic import BaseModel, Field, validator, root_validator

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


class ThresholdParameterSet(BaseModel):
    base: Dict[str, List[Any]] = Field(
        default_factory=dict,
        description="Primary threshold grids such as long/short triggers",
    )
    features: Dict[str, Dict[str, List[Any]]] = Field(
        default_factory=dict,
        description="Feature-specific threshold grids keyed by metric",
    )

    if PYDANTIC_V2:  # pragma: no branch

        @model_validator(mode="before")
        def transform_legacy(cls, values: Any) -> Any:
            if isinstance(values, dict) and "base" not in values and "features" not in values:
                return {"base": values, "features": {}}
            return values

        @field_validator("base")
        def validate_base(cls, value: Dict[str, List[Any]]) -> Dict[str, List[Any]]:
            if not value:
                raise ValueError("base parameter grid must include at least one entry")
            for name, candidates in value.items():
                if not isinstance(candidates, list) or not candidates:
                    raise ValueError(f"base parameter '{name}' must define at least one candidate value")
            return value

        @field_validator("features")
        def validate_features(
            cls, value: Dict[str, Dict[str, List[Any]]]
        ) -> Dict[str, Dict[str, List[Any]]]:
            for metric, grid in value.items():
                if not isinstance(grid, dict):
                    raise ValueError(f"feature '{metric}' must define a parameter map")
                for name, candidates in grid.items():
                    if not isinstance(candidates, list) or not candidates:
                        raise ValueError(
                            f"feature '{metric}' parameter '{name}' must define at least one candidate value"
                        )
            return value

    else:

        @root_validator(pre=True)  # type: ignore[misc]
        def transform_legacy(cls, values: Dict[str, Any]) -> Dict[str, Any]:
            if isinstance(values, dict) and "base" not in values and "features" not in values:
                return {"base": values, "features": {}}
            return values

        @validator("base")  # type: ignore[misc]
        def validate_base(cls, value: Dict[str, List[Any]]) -> Dict[str, List[Any]]:
            if not value:
                raise ValueError("base parameter grid must include at least one entry")
            for name, candidates in value.items():
                if not isinstance(candidates, list) or not candidates:
                    raise ValueError(f"base parameter '{name}' must define at least one candidate value")
            return value

        @validator("features")  # type: ignore[misc]
        def validate_features(
            cls, value: Dict[str, Dict[str, List[Any]]]
        ) -> Dict[str, Dict[str, List[Any]]]:
            for metric, grid in value.items():
                if not isinstance(grid, dict):
                    raise ValueError(f"feature '{metric}' must define a parameter map")
                for name, candidates in grid.items():
                    if not isinstance(candidates, list) or not candidates:
                        raise ValueError(
                            f"feature '{metric}' parameter '{name}' must define at least one candidate value"
                        )
            return value

    def flatten(self) -> Dict[str, List[Any]]:
        flat: Dict[str, List[Any]] = {}
        for name, candidates in self.base.items():
            flat[name] = list(candidates)
        for metric, grid in self.features.items():
            for name, candidates in grid.items():
                flat[f"{metric}__{name}"] = list(candidates)
        return flat


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
    base_metrics: List[str] = Field(default_factory=list, description="Primary indicators to optimise")
    feature_metrics: List[str] = Field(default_factory=list, description="Event or feature indicators to optimise")
    parameters: ThresholdParameterSet
    algorithm: OptimizationAlgorithm
    start_date: date
    end_date: date
    frequency_minutes: int = Field(..., ge=0, description="Interval in minutes for generating reports")
    iterations: Optional[int] = Field(None, ge=1)

    if PYDANTIC_V2:  # pragma: no branch

        @field_validator("base_metrics", "feature_metrics")
        def normalize_metric_lists(cls, values: List[str]) -> List[str]:
            cleaned = []
            seen = set()
            for value in values or []:
                trimmed = value.strip()
                if not trimmed or trimmed in seen:
                    continue
                cleaned.append(trimmed)
                seen.add(trimmed)
            return cleaned

        @field_validator("end_date")
        def validate_plan_dates(cls, v: date, info: FieldValidationInfo) -> date:
            start = info.data.get("start_date")
            if start and v < start:
                raise ValueError("end_date must not be earlier than start_date")
            return v

        @model_validator(mode="after")
        def ensure_metrics_present(cls, values: "OptimizationPlanBase") -> "OptimizationPlanBase":
            if not values.base_metrics and not values.feature_metrics:
                raise ValueError("at least one base or feature metric must be selected")
            return values

    else:

        @validator("base_metrics", "feature_metrics")  # type: ignore[misc]
        def normalize_metric_lists(cls, v: List[str]) -> List[str]:
            cleaned: List[str] = []
            seen = set()
            for value in v or []:
                trimmed = value.strip()
                if not trimmed or trimmed in seen:
                    continue
                cleaned.append(trimmed)
                seen.add(trimmed)
            return cleaned

        @validator("end_date")  # type: ignore[misc]
        def validate_plan_dates(cls, v: date, values: Dict[str, Any]) -> date:
            start = values.get("start_date")
            if start and v < start:
                raise ValueError("end_date must not be earlier than start_date")
            return v

        @root_validator  # type: ignore[misc]
        def ensure_metrics_present(cls, values: Dict[str, Any]) -> Dict[str, Any]:
            base = values.get("base_metrics") or []
            features = values.get("feature_metrics") or []
            if not base and not features:
                raise ValueError("at least one base or feature metric must be selected")
            return values


class OptimizationPlanCreate(OptimizationPlanBase):
    pass


class OptimizationPlanUpdate(BaseModel):
    name: Optional[str] = None
    strategy: Optional[str] = None
    symbol: Optional[str] = None
    base_metrics: Optional[List[str]] = None
    feature_metrics: Optional[List[str]] = None
    parameters: Optional[ThresholdParameterSet] = None
    algorithm: Optional[OptimizationAlgorithm] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    frequency_minutes: Optional[int] = None
    iterations: Optional[int] = None

    if PYDANTIC_V2:  # pragma: no branch

        @field_validator("base_metrics", "feature_metrics")
        def normalize_metrics(
            cls, values: Optional[List[str]]
        ) -> Optional[List[str]]:
            if values is None:
                return None
            cleaned = []
            seen = set()
            for value in values:
                trimmed = value.strip()
                if not trimmed or trimmed in seen:
                    continue
                cleaned.append(trimmed)
                seen.add(trimmed)
            if not cleaned:
                raise ValueError("metric list must not be empty")
            return cleaned

        @field_validator("end_date")
        def validate_plan_dates(cls, v: Optional[date], info: FieldValidationInfo):
            start = info.data.get("start_date")
            if v and start and v < start:
                raise ValueError("end_date must not be earlier than start_date")
            return v

    else:

        @validator("base_metrics", "feature_metrics")  # type: ignore[misc]
        def normalize_metrics(
            cls, v: Optional[List[str]]
        ):  # pylint: disable=unused-argument
            if v is None:
                return v
            cleaned: List[str] = []
            seen = set()
            for value in v:
                trimmed = value.strip()
                if not trimmed or trimmed in seen:
                    continue
                cleaned.append(trimmed)
                seen.add(trimmed)
            if not cleaned:
                raise ValueError("metric list must not be empty")
            return cleaned

        @validator("end_date")  # type: ignore[misc]
        def validate_plan_dates(cls, v: Optional[date], values: Dict[str, Any]):
            start = values.get("start_date")
            if v and start and v < start:
                raise ValueError("end_date must not be earlier than start_date")
            return v


class OptimizationPlanResponse(OptimizationPlanBase):
    id: str
    metrics: List[str] = Field(default_factory=list)
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
    base_metrics: List[str]
    feature_metrics: List[str]
    algorithms: List[str]


class OptimizerDataRangeResponse(BaseModel):
    symbol: str
    start_date: date
    end_date: date
    records: Optional[int]


def as_threshold_parameter_set(value: Any) -> ThresholdParameterSet:
    if isinstance(value, ThresholdParameterSet):
        return value
    if PYDANTIC_V2:  # pragma: no branch
        return ThresholdParameterSet.model_validate(value)
    return ThresholdParameterSet.parse_obj(value)
