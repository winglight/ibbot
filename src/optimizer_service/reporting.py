"""Utility helpers for optimizer report enrichment."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Dict, List

from .schemas import OptimizationResultItem


def compute_runtime_summary(
    started_at: datetime,
    completed_at: datetime,
    elapsed_seconds: float,
    estimated_total: int,
    actual_total: int,
) -> Dict[str, Any]:
    """Build a runtime summary dictionary used in report cards."""

    actual_duration_raw = max(elapsed_seconds, 0.001)
    per_combo = actual_duration_raw / actual_total if actual_total else 0.0
    expected_duration_raw = per_combo * max(estimated_total, 0)
    tests_per_second_raw = (
        (actual_total / actual_duration_raw) if actual_duration_raw and actual_total else 0.0
    )
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


def compute_return_distribution(results: List[OptimizationResultItem]) -> Dict[str, float]:
    """Calculate percentile-based annual return distribution for a report."""

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


def compute_return_histogram(
    results: List[OptimizationResultItem], bins: int = 12
) -> List[Dict[str, float | int]]:
    """Build a histogram of annual returns for visual summaries.

    The histogram groups annual returns into equally sized bins so that the
    frontend can render a compact bar chart instead of enumerating every
    optimisation result individually.
    """

    if bins <= 0:
        bins = 1

    returns = [
        item.metrics.get("annual_return")
        for item in results
        if item.metrics and item.metrics.get("annual_return") is not None
    ]
    if not returns:
        return []

    returns.sort()
    minimum = returns[0]
    maximum = returns[-1]
    if math.isclose(minimum, maximum):
        return [
            {
                "start": round(minimum, 4),
                "end": round(maximum, 4),
                "count": len(returns),
            }
        ]

    bin_count = min(bins, max(1, len(returns)))
    span = maximum - minimum
    step = span / bin_count if bin_count else span
    if math.isclose(step, 0.0):
        step = span or 1.0

    counts = [0 for _ in range(bin_count)]
    for value in returns:
        index = int((value - minimum) / step)
        if index >= bin_count:
            index = bin_count - 1
        counts[index] += 1

    histogram: List[Dict[str, float | int]] = []
    for idx, count in enumerate(counts):
        start = minimum + step * idx
        end = start + step
        histogram.append(
            {
                "start": round(start, 4),
                "end": round(end, 4),
                "count": count,
            }
        )
    return histogram


def build_conclusion(
    total: int,
    success: int,
    failed: int,
    best_sharpe: float | None,
    best_drawdown: float | None,
    distribution: Dict[str, float],
) -> str:
    """Generate a human readable conclusion for the optimizer report."""

    if total <= 0:
        return "未运行任何组合，无法得出结论。"

    summary_parts = []
    base_text = f"共测试 {total} 组组合，其中成功 {success} 组"
    if failed:
        base_text += f"，失败 {failed} 组"
    summary_parts.append(base_text + "。")

    distribution_text = _format_distribution_sentence(distribution)
    if distribution_text:
        summary_parts.append(distribution_text)

    decision = _build_decision_sentence(
        total,
        success,
        failed,
        best_sharpe,
        best_drawdown,
        distribution,
    )
    if decision:
        summary_parts.append(decision)

    return " ".join(part for part in summary_parts if part)


def _format_distribution_sentence(distribution: Dict[str, float]) -> str:
    if not distribution:
        return "收益率分布信息不足，建议补充更多历史样本。"
    minimum = distribution.get("min")
    maximum = distribution.get("max")
    median = distribution.get("p50")
    mean = distribution.get("mean")
    pieces = []
    if minimum is not None and maximum is not None:
        pieces.append(
            f"收益率区间 {_format_percentage(minimum)} ~ {_format_percentage(maximum)}"
        )
    if median is not None:
        pieces.append(f"中位数 {_format_percentage(median)}")
    if mean is not None:
        pieces.append(f"平均值 {_format_percentage(mean)}")
    if not pieces:
        return "收益率分布信息不足，建议补充更多历史样本。"
    return "，".join(pieces) + "。"


def _build_decision_sentence(
    total: int,
    success: int,
    failed: int,
    best_sharpe: float | None,
    best_drawdown: float | None,
    distribution: Dict[str, float],
) -> str:
    if success <= 1:
        return (
            f"当前样本偏少，仅 {success} 组组合成功，建议扩充参数范围或延长运行时间。"
        )

    if failed:
        failure_ratio = failed / total if total else 0.0
        if failure_ratio >= 0.4:
            return (
                f"共有 {failed} 组组合执行失败，占比 {failure_ratio:.0%}，需排查失败原因或过滤不稳定参数。"
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
            return "收益率分布分散度较小，可重点关注收益与回撤均衡表现的前列组合。"

    if failed:
        return f"整体表现稳定，但仍有 {failed} 组组合未通过，建议复核失败组合的触发条件。"

    return "组合表现稳健，可重点评估夏普与回撤表现优异的候选组合。"


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
