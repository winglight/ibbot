"""Analytics helpers for optimizer reporting."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from statistics import mean, median
from typing import Dict, List, Optional, Set

from .plan_manager import PlanManager
from .schemas import (
    MetricAggregate,
    ParameterGroupPerformance,
    ParameterGroupPerformanceResponse,
    ParameterGroupPlanRef,
    ParameterGroupStats,
    TaskState,
)
from .storage import OptimizerTaskStore


def summarize_parameter_groups(
    plan_manager: PlanManager,
    task_store: OptimizerTaskStore,
    *,
    top_n: int = 20,
) -> ParameterGroupPerformanceResponse:
    """Aggregate performance metrics for parameter groups across tasks."""

    entries: List[_ResultEntry] = []
    for task_id, record in task_store.iter_tasks():
        if record.state != TaskState.COMPLETED:
            continue
        if not record.report or not record.report.results:
            continue

        symbol: Optional[str] = None
        plan_name: Optional[str] = None
        plan_id: Optional[str] = record.plan_id
        if plan_id:
            try:
                plan = plan_manager.get_plan(plan_id)
            except KeyError:
                plan = None
            if plan:
                symbol = plan.symbol
                plan_name = plan.name

        if not symbol:
            # Without an associated instrument the group comparison loses context.
            continue

        strategy = record.request.strategy
        for result in record.report.results:
            parameters = _normalize_parameters(result.parameters)
            sharpe = _safe_float(result.metrics.get("sharpe"))
            drawdown = _safe_float(result.metrics.get("max_drawdown"))
            annual_return = _safe_float(result.metrics.get("annual_return"))
            if sharpe is None and drawdown is None and annual_return is None:
                continue
            group_id = _build_group_key(symbol, strategy, parameters)
            entries.append(
                _ResultEntry(
                    group_id=group_id,
                    symbol=symbol,
                    strategy=strategy,
                    parameters=parameters,
                    plan_id=plan_id,
                    plan_name=plan_name,
                    task_id=task_id,
                    sharpe=sharpe,
                    drawdown=drawdown,
                    annual_return=annual_return,
                )
            )

    if not entries:
        return ParameterGroupPerformanceResponse(groups=[])

    entries.sort(
        key=lambda item: (
            item.annual_return if item.annual_return is not None else float("-inf"),
            item.sharpe if item.sharpe is not None else float("-inf"),
        ),
        reverse=True,
    )

    selected_group_ids: Set[str] = set()
    ordered_group_ids: List[str] = []
    unique_group_ids = {entry.group_id for entry in entries}
    if top_n <= 0:
        max_groups = len(unique_group_ids)
    else:
        max_groups = min(top_n, len(unique_group_ids))

    for entry in entries:
        if entry.group_id in selected_group_ids:
            continue
        if entry.annual_return is None:
            continue
        selected_group_ids.add(entry.group_id)
        ordered_group_ids.append(entry.group_id)
        if len(ordered_group_ids) >= max_groups:
            break

    if len(ordered_group_ids) < max_groups:
        for entry in entries:
            if entry.group_id in selected_group_ids:
                continue
            selected_group_ids.add(entry.group_id)
            ordered_group_ids.append(entry.group_id)
            if len(ordered_group_ids) >= max_groups:
                break

    buckets: Dict[str, _GroupBucket] = {}
    for entry in entries:
        if entry.group_id not in selected_group_ids:
            continue
        bucket = buckets.setdefault(
            entry.group_id,
            _GroupBucket(
                symbol=entry.symbol,
                strategy=entry.strategy,
                parameters=entry.parameters,
            ),
        )
        bucket.add_sample(
            task_id=entry.task_id,
            plan_id=entry.plan_id,
            plan_name=entry.plan_name,
            sharpe=entry.sharpe,
            drawdown=entry.drawdown,
            annual_return=entry.annual_return,
        )

    performances: List[ParameterGroupPerformance] = []
    for group_id in ordered_group_ids:
        bucket = buckets.get(group_id)
        if not bucket or not bucket.sample_size:
            continue
        stats = bucket.compute_stats()
        plan_refs = [
            ParameterGroupPlanRef(id=plan_id, name=bucket.plan_names[plan_id])
            for plan_id in sorted(bucket.plan_names)
        ]
        performances.append(
            ParameterGroupPerformance(
                group_id=group_id,
                symbol=bucket.symbol,
                strategy=bucket.strategy,
                parameters=bucket.parameters,
                sample_size=bucket.sample_size,
                task_ids=sorted(bucket.task_ids),
                plans=plan_refs,
                stats=ParameterGroupStats(
                    sharpe=stats["sharpe"],
                    max_drawdown=stats["drawdown"],
                    annual_return=stats["return"],
                ),
            )
        )

    performances.sort(
        key=lambda item: (
            item.stats.annual_return.average
            if item.stats.annual_return.average is not None
            else float("-inf"),
            item.stats.sharpe.average if item.stats.sharpe.average is not None else float("-inf"),
            item.sample_size,
        ),
        reverse=True,
    )
    if top_n > 0:
        performances = performances[: top_n]
    return ParameterGroupPerformanceResponse(groups=performances)


@dataclass
class _ResultEntry:
    group_id: str
    symbol: str
    strategy: str
    parameters: Dict[str, object]
    plan_id: Optional[str]
    plan_name: Optional[str]
    task_id: str
    sharpe: Optional[float]
    drawdown: Optional[float]
    annual_return: Optional[float]


@dataclass
class _GroupBucket:
    symbol: str
    strategy: str
    parameters: Dict[str, object]
    task_ids: Set[str] = field(default_factory=set)
    plan_names: Dict[str, str] = field(default_factory=dict)
    sharpe_values: List[float] = field(default_factory=list)
    drawdown_values: List[float] = field(default_factory=list)
    return_values: List[float] = field(default_factory=list)
    sample_size: int = 0

    def add_sample(
        self,
        *,
        task_id: str,
        plan_id: Optional[str],
        plan_name: Optional[str],
        sharpe: Optional[float],
        drawdown: Optional[float],
        annual_return: Optional[float],
    ) -> None:
        recorded = False
        if sharpe is not None:
            self.sharpe_values.append(sharpe)
            recorded = True
        if drawdown is not None:
            self.drawdown_values.append(drawdown)
            recorded = True
        if annual_return is not None:
            self.return_values.append(annual_return)
            recorded = True
        if recorded:
            self.sample_size += 1
        self.task_ids.add(task_id)
        if plan_id:
            self.plan_names[plan_id] = plan_name or plan_id

    def compute_stats(self) -> Dict[str, MetricAggregate]:
        return {
            "sharpe": _aggregate_metric(self.sharpe_values),
            "drawdown": _aggregate_metric(self.drawdown_values),
            "return": _aggregate_metric(self.return_values),
        }


def _normalize_parameters(parameters: Dict[str, object]) -> Dict[str, object]:
    # Ensure stable ordering for hashing and presentation.
    return {key: parameters[key] for key in sorted(parameters)}


def _aggregate_metric(values: List[float]) -> MetricAggregate:
    if not values:
        return MetricAggregate()
    return MetricAggregate(
        average=round(mean(values), 6),
        median=round(median(values), 6),
    )


def _safe_float(value: object) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        multiplier = 1.0
        if stripped.endswith("%"):
            stripped = stripped[:-1]
            multiplier = 0.01
        try:
            numeric = float(stripped)
        except ValueError:
            return None
        return numeric * multiplier
    return None


def _build_group_key(symbol: str, strategy: str, parameters: Dict[str, object]) -> str:
    serial = json.dumps(
        {"symbol": symbol, "strategy": strategy, "parameters": parameters},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha1(serial.encode("utf-8")).hexdigest()
