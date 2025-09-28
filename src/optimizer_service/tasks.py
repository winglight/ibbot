"""Background task implementations for optimizer module."""

from __future__ import annotations
from datetime import datetime, timedelta
from time import perf_counter
from typing import Any, Dict, List, Tuple

from loguru import logger

from .engine import OptimizationEngine
from .reporting import (
    build_conclusion,
    compute_return_distribution,
    compute_return_histogram,
    compute_runtime_summary,
)
from .schemas import (
    OptimizationReportData,
    OptimizationRequest,
    OptimizationResultItem,
    TaskState,
)
from .storage import OptimizerTaskStore


def _store_is_stop_requested(store: OptimizerTaskStore, task_id: str) -> bool:
    checker = getattr(store, "is_stop_requested", None)
    if callable(checker):
        try:
            return bool(checker(task_id))
        except Exception:
            return False
    return False


def _store_mark_stopped(store: OptimizerTaskStore, task_id: str, progress: int) -> None:
    marker = getattr(store, "mark_stopped", None)
    if not callable(marker):
        return
    try:
        marker(task_id, progress=progress)
    except TypeError:
        # Some lightweight test doubles expose ``mark_stopped`` without keyword
        # arguments.  Fall back to positional invocation so that the task can
        # still be marked as stopped.
        marker(task_id, progress)
    except Exception:
        # Defensive guard – best-effort update only.
        pass


def run_optimization_task(task_id: str, request: OptimizationRequest, store: OptimizerTaskStore) -> None:
    """Execute optimization workflow (placeholder implementation).

    At this stage we are still using simulated results, but the structure mirrors the
    production workflow: iterate combinations, capture failures, compute summary
    statistics, and build equity data for the report.
    """

    logger = _task_logger(task_id)
    logger.info("Starting optimization task")
    initial_progress = 5
    store.update_state(task_id, TaskState.RUNNING, progress=initial_progress, message="running")

    try:
        engine = OptimizationEngine(request)
        total = max(engine.estimated_total(), 1)
        results: List[OptimizationResultItem] = []
        fail_reasons: List[str] = []
        unique_fail_reasons: set[str] = set()
        failed_combination_count = 0
        best_result: Tuple[float, OptimizationResultItem] | None = None
        started_at = datetime.utcnow()
        start_perf = perf_counter()
        progress_value = initial_progress

        for idx, outcome in enumerate(engine.run(), start=1):
            if _store_is_stop_requested(store, task_id):
                _store_mark_stopped(store, task_id, progress_value)
                logger.info("Stop requested, aborting optimization task")
                return
            progress = min(95, max(10, int(idx / total * 90)))
            progress_value = progress
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
                failed_combination_count += 1
                reason = outcome.fail_reason or "未知原因"
                formatted_reason = _format_fail_reason(reason, outcome.parameters)
                if formatted_reason not in unique_fail_reasons:
                    unique_fail_reasons.add(formatted_reason)
                    fail_reasons.append(formatted_reason)
                store.append_fail_reason(task_id, reason)

        if not results:
            if _store_is_stop_requested(store, task_id):
                _store_mark_stopped(store, task_id, progress_value)
                logger.info("Stop requested before completion, task marked as stopped")
                return
            message = "所有组合执行失败"
            store.fail_task(task_id, message)
            logger.warning(message)
            return

        completed_at = datetime.utcnow()
        elapsed_seconds = max(perf_counter() - start_perf, 0.0)

        if _store_is_stop_requested(store, task_id):
            _store_mark_stopped(store, task_id, progress_value)
            logger.info("Stop requested after processing combinations, task marked as stopped")
            return

        best_item = best_result[1] if best_result else results[0]
        best_metrics = best_item.metrics
        equity_curve = _build_equity_curve(
            request,
            best_metrics.get("annual_return", 0.0),
            best_metrics.get("max_drawdown", 0.0),
        )
        sorted_results = sorted(results, key=lambda r: r.metrics.get("sharpe", 0.0), reverse=True)
        actual_total = len(results) + failed_combination_count
        runtime_summary = compute_runtime_summary(
            started_at,
            completed_at,
            elapsed_seconds,
            total,
            actual_total,
        )
        runtime_summary["status"] = TaskState.COMPLETED.value
        return_distribution = compute_return_distribution(sorted_results)
        return_histogram = compute_return_histogram(sorted_results)
        report = OptimizationReportData(
            task_id=task_id,
            results=sorted_results,
            summary={
                "best_sharpe": round(best_metrics.get("sharpe", 0.0), 3),
                "best_max_drawdown": round(best_metrics.get("max_drawdown", 0.0), 4),
                "best_annual_return": round(best_metrics.get("annual_return", 0.0), 4),
                "tested_combinations": actual_total,
                "successful_combinations": len(results),
                "failed_combinations": failed_combination_count,
                "generated_at": datetime.utcnow().isoformat() + "Z",
                "runtime": runtime_summary,
                "annual_return_distribution": return_distribution,
                "annual_return_histogram": return_histogram,
                "conclusion": build_conclusion(
                    actual_total,
                    len(results),
                    len(fail_reasons),
                    best_metrics.get("sharpe"),
                    best_metrics.get("max_drawdown"),
                    return_distribution,
                ),
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


def _compute_runtime_summary(
    started_at: datetime,
    completed_at: datetime,
    elapsed_seconds: float,
    estimated_total: int,
    actual_total: int,
) -> Dict[str, Any]:
    actual_duration_raw = max(elapsed_seconds, 0.001)
    per_combo = actual_duration_raw / actual_total if actual_total else 0.0
    expected_duration_raw = per_combo * max(estimated_total, 0)
    tests_per_second_raw = (actual_total / actual_duration_raw) if actual_duration_raw and actual_total else 0.0
    actual_duration = round(actual_duration_raw, 2)
    expected_duration = round(expected_duration_raw, 2)
    tests_per_second = round(tests_per_second_raw, 2) if tests_per_second_raw else 0.0
    if actual_total and actual_duration <= 0:
        actual_duration = 0.01
    if expected_duration <= 0 and expected_duration_raw > 0:
        expected_duration = 0.01
    return {
        "started_at": started_at.isoformat() + "Z",
        "completed_at": completed_at.isoformat() + "Z",
        "actual_duration_seconds": actual_duration,
        "expected_duration_seconds": expected_duration,
        "tests_per_second": tests_per_second,
        "estimated_total_combinations": int(estimated_total),
        "actual_total_combinations": int(actual_total),
    }


def _compute_return_distribution(results: List[OptimizationResultItem]) -> Dict[str, float]:
    returns = [
        item.metrics.get("annual_return")
        for item in results
        if item.metrics and item.metrics.get("annual_return") is not None
    ]
    if not returns:
        return {}
    returns.sort()
    count = len(returns)
    average = sum(returns) / count
    return {
        "min": round(returns[0], 4),
        "max": round(returns[-1], 4),
        "mean": round(average, 4),
        "p25": round(_percentile(returns, 0.25), 4),
        "p50": round(_percentile(returns, 0.5), 4),
        "p75": round(_percentile(returns, 0.75), 4),
    }


def _percentile(sorted_values: List[float], quantile: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    position = (len(sorted_values) - 1) * quantile
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    lower_value = sorted_values[lower_index]
    upper_value = sorted_values[upper_index]
    if lower_index == upper_index:
        return float(lower_value)
    weight = position - lower_index
    return float(lower_value + (upper_value - lower_value) * weight)


def _format_percentage(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.1f}%"


def _build_conclusion(
    total: int,
    success: int,
    failed: int,
    best_sharpe: float | None,
    best_drawdown: float | None,
    distribution: Dict[str, float],
) -> str:
    if total <= 0:
        return "未运行任何组合，无法得出结论。"
    if success <= 1:
        return f"测试样本偏少（共 {total} 组，成功 {success} 组），建议扩充组合数量。"

    if failed:
        failure_ratio = failed / total if total else 0.0
        if failure_ratio >= 0.4:
            return (
                f"共有 {failed} 组组合执行失败，占比 {failure_ratio:.0%}，需排查失败原因或过滤不稳定的参数组合。"
            )

    if best_sharpe is not None and best_sharpe < 1.0:
        return (
            f"最佳组合夏普比率仅 {best_sharpe:.2f}，风险调整收益不足，建议优化指标阈值或增加风险过滤。"
        )

    if best_drawdown is not None and best_drawdown > 0.15:
        return (
            f"最佳组合最大回撤 {_format_percentage(best_drawdown)}，风险偏高，可优先剔除高回撤组合。"
        )

    median_return = distribution.get("p50") if distribution else None
    if median_return is not None:
        if median_return < 0.08:
            return (
                f"收益率分布偏低（中位数 {_format_percentage(median_return)}），建议延长样本区间或调整参数范围。"
            )
        p75 = distribution.get("p75")
        p25 = distribution.get("p25")
        if p75 is not None and p25 is not None and (p75 - p25) < 0.05:
            return "收益率分布集中且波动较小，可重点关注收益与回撤均衡表现的前列组合。"

    if failed:
        return f"整体表现稳定，但仍有 {failed} 组组合未通过，建议复核失败组合的触发条件。"

    return "组合表现稳健，可重点评估夏普与回撤表现优异的候选组合。"
