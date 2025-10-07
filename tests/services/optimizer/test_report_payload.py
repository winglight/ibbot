"""Tests for report payload compaction helpers."""

from __future__ import annotations

from datetime import date

import pytest

from src.optimizer_service.routes import _build_report_payload
from src.optimizer_service.schemas import (
    OptimizationAlgorithm,
    OptimizationReportData,
    OptimizationRequest,
    OptimizationResultItem,
    TaskState,
)
from src.optimizer_service.storage import TaskRecord


@pytest.fixture()
def optimization_request() -> OptimizationRequest:
    return OptimizationRequest(
        strategy="demo-strategy",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 31),
        parameters={"alpha": [1, 2]},
        algorithm=OptimizationAlgorithm.GRID,
    )


def _make_result(idx: int) -> OptimizationResultItem:
    return OptimizationResultItem(
        parameters={"p": idx},
        metrics={
            "annual_return": 0.05 * idx,
            "sharpe": float(idx),
            "max_drawdown": 0.01 * idx,
        },
    )


def test_build_report_payload_compacts_results_and_failures(
    optimization_request: OptimizationRequest,
) -> None:
    record = TaskRecord(request=optimization_request, state=TaskState.COMPLETED)
    record.fail_reasons = ["信号数不足", "信号数不足", "数据缺失"]

    report = OptimizationReportData(
        task_id="task-123",
        results=[_make_result(idx) for idx in range(5)],
        summary={},
        equity_curve=[{"timestamp": "2024-01-01", "equity": 1.0}],
        fail_reasons=["p=0: 信号数不足", "p=1: 信号数不足", "p=2: 数据缺失"],
    )
    record.report = report

    payload = _build_report_payload(record)

    assert payload is not None
    assert payload.task_id == "task-123"
    assert [item.parameters for item in payload.results] == [
        {"p": 0},
        {"p": 1},
        {"p": 2},
    ]
    assert payload.summary["successful_combinations"] == 5
    assert payload.summary["failed_combinations"] == 3
    assert payload.summary["tested_combinations"] == 8
    assert payload.summary["detailed_result_limit"] == 3
    assert payload.summary["other_combination_count"] == 2
    assert payload.summary["annual_return_histogram"]
    assert payload.summary["max_drawdown_histogram"]
    assert payload.summary["sharpe_histogram"]
    assert payload.summary["max_drawdown_distribution"]["mean"] is not None
    assert payload.summary["sharpe_distribution"]["mean"] is not None
    assert payload.fail_reasons == ["信号数不足", "数据缺失"]


def test_build_report_payload_respects_existing_summary_fields(
    optimization_request: OptimizationRequest,
) -> None:
    record = TaskRecord(request=optimization_request, state=TaskState.COMPLETED)
    record.fail_reasons = []

    precomputed_summary = {
        "successful_combinations": "7",
        "failed_combinations": "1",
        "tested_combinations": 42,
        "annual_return_histogram": [42],
        "max_drawdown_histogram": [13],
        "sharpe_histogram": [7],
        "annual_return_distribution": {"mean": 1.23},
        "max_drawdown_distribution": {"mean": 0.42},
        "sharpe_distribution": {"mean": 0.99},
    }
    report = OptimizationReportData(
        task_id="task-456",
        results=[_make_result(idx) for idx in range(2)],
        summary=precomputed_summary,
        equity_curve=[],
        fail_reasons=["数据缺失"],
    )
    record.report = report

    payload = _build_report_payload(record)

    assert payload is not None
    summary = payload.summary
    assert summary["successful_combinations"] == 7
    assert summary["failed_combinations"] == 1
    assert summary["tested_combinations"] == 42
    assert summary["detailed_result_limit"] == 3
    assert summary["other_combination_count"] == 4
    assert summary["annual_return_histogram"] == [42]
    assert summary["max_drawdown_histogram"] == [13]
    assert summary["sharpe_histogram"] == [7]
    assert summary["annual_return_distribution"] == {"mean": 1.23}
    assert summary["max_drawdown_distribution"] == {"mean": 0.42}
    assert summary["sharpe_distribution"] == {"mean": 0.99}
    assert payload.fail_reasons == ["数据缺失"]
