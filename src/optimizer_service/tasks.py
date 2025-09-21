"""Background task implementations for optimizer module."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Tuple

from loguru import logger

from .engine import OptimizationEngine
from .schemas import (
    OptimizationReportData,
    OptimizationRequest,
    OptimizationResultItem,
    TaskState,
)
from .storage import OptimizerTaskStore


def run_optimization_task(task_id: str, request: OptimizationRequest, store: OptimizerTaskStore) -> None:
    """Execute optimization workflow (placeholder implementation).

    At this stage we are still using simulated results, but the structure mirrors the
    production workflow: iterate combinations, capture failures, compute summary
    statistics, and build equity data for the report.
    """

    logger = _task_logger(task_id)
    logger.info("Starting optimization task")
    store.update_state(task_id, TaskState.RUNNING, progress=5, message="running")

    try:
        engine = OptimizationEngine(request)
        total = max(engine.estimated_total(), 1)
        results: List[OptimizationResultItem] = []
        fail_reasons: List[str] = []
        best_result: Tuple[float, OptimizationResultItem] | None = None

        for idx, outcome in enumerate(engine.run(), start=1):
            progress = min(95, max(10, int(idx / total * 90)))
            store.update_state(
                task_id,
                TaskState.RUNNING,
                progress=progress,
                message=f"processed {idx}/{total}",
            )
            if outcome.success and outcome.result:
                metric_sharpe = outcome.result.metrics.get("sharpe", 0.0)
                results.append(outcome.result)
                if (best_result is None) or metric_sharpe > best_result[0]:
                    best_result = (metric_sharpe, outcome.result)
            else:
                reason = outcome.fail_reason or "未知原因"
                fail_reasons.append(_format_fail_reason(reason, outcome.parameters))
                store.append_fail_reason(task_id, reason)

        if not results:
            message = "所有组合执行失败"
            store.fail_task(task_id, message)
            logger.warning(message)
            return

        best_item = best_result[1] if best_result else results[0]
        best_metrics = best_item.metrics
        equity_curve = _build_equity_curve(
            request,
            best_metrics.get("annual_return", 0.0),
            best_metrics.get("max_drawdown", 0.0),
        )
        sorted_results = sorted(results, key=lambda r: r.metrics.get("sharpe", 0.0), reverse=True)
        report = OptimizationReportData(
            task_id=task_id,
            results=sorted_results,
            summary={
                "best_sharpe": round(best_metrics.get("sharpe", 0.0), 3),
                "best_max_drawdown": round(best_metrics.get("max_drawdown", 0.0), 4),
                "best_annual_return": round(best_metrics.get("annual_return", 0.0), 4),
                "tested_combinations": len(results) + len(fail_reasons),
                "successful_combinations": len(results),
                "failed_combinations": len(fail_reasons),
                "generated_at": datetime.utcnow().isoformat() + "Z",
            },
            equity_curve=equity_curve,
            fail_reasons=fail_reasons,
        )
        store.complete_task(task_id, report)
        logger.info("Optimization task completed")
    except Exception as exc:  # pragma: no cover - defensive
        store.fail_task(task_id, f"task failed: {exc}")
        logger.exception("Optimization task failed")


def _task_logger(task_id: str):
    return logger.bind(module="optimizer", task_id=task_id)


def _format_fail_reason(reason: str, parameters: Dict[str, Any]) -> str:
    if parameters:
        param_desc = ", ".join(f"{k}={v}" for k, v in sorted(parameters.items()))
        return f"{param_desc}: {reason}"
    return reason


def _build_equity_curve(request: OptimizationRequest, annual_return: float, max_drawdown: float) -> List[Dict[str, float | str]]:
    total_days = max(1, (request.end_date - request.start_date).days)
    steps = min(20, total_days)
    interval = max(1, total_days // steps)
    curve: List[Dict[str, float | str]] = []
    base_equity = 1.0
    target_equity = max(0.5, base_equity + annual_return)
    drawdown_depth = max(0.0, min(max_drawdown, 0.9))
    for day in range(0, total_days + 1, interval):
        progress = day / total_days if total_days else 1.0
        equity = base_equity + (target_equity - base_equity) * progress
        if 0.3 <= progress <= 0.6:
            equity -= drawdown_depth * 0.5 * (1 - abs(0.45 - progress) / 0.15)
        current_date = request.start_date + timedelta(days=day)
        curve.append({
            "timestamp": current_date.isoformat(),
            "equity": round(equity, 4),
        })
    if curve[-1]["timestamp"] != request.end_date.isoformat():
        curve.append({
            "timestamp": request.end_date.isoformat(),
            "equity": round(target_equity, 4),
        })
    return curve
