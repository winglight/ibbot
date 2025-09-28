from datetime import date

from src.optimizer_service.routes import _build_report_payload
from src.optimizer_service.schemas import (
    OptimizationAlgorithm,
    OptimizationReportData,
    OptimizationRequest,
    OptimizationResultItem,
    TaskState,
)
from src.optimizer_service.storage import TaskRecord


def _make_request() -> OptimizationRequest:
    return OptimizationRequest(
        strategy="demo-strategy",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 31),
        parameters={"alpha": [1, 2]},
        algorithm=OptimizationAlgorithm.GRID,
    )


def test_build_report_payload_compacts_results_and_failures() -> None:
    record = TaskRecord(request=_make_request(), state=TaskState.COMPLETED)
    results = [
        OptimizationResultItem(
            parameters={"p": idx},
            metrics={"annual_return": 0.05 * idx, "sharpe": float(idx), "max_drawdown": 0.01 * idx},
        )
        for idx in range(5)
    ]
    report = OptimizationReportData(
        task_id="task-123",
        results=results,
        summary={},
        equity_curve=[{"timestamp": "2024-01-01", "equity": 1.0}],
        fail_reasons=["p=0: 信号数不足", "p=1: 信号数不足", "p=2: 数据缺失"],
    )
    record.report = report
    record.fail_reasons = ["信号数不足", "信号数不足", "数据缺失"]

    payload = _build_report_payload(record)

    assert payload is not None
    assert len(payload.results) == 3
    assert payload.summary["successful_combinations"] == 5
    assert payload.summary["failed_combinations"] == 3
    assert payload.summary["tested_combinations"] == 8
    assert payload.summary["other_combination_count"] == 2
    assert payload.summary["detailed_result_limit"] == 3
    assert len(payload.summary["annual_return_histogram"]) >= 1
    assert len(payload.summary["max_drawdown_histogram"]) >= 1
    assert len(payload.summary["sharpe_histogram"]) >= 1
    assert payload.summary["max_drawdown_distribution"]["mean"] is not None
    assert payload.summary["sharpe_distribution"]["mean"] is not None
    assert payload.fail_reasons == ["信号数不足", "数据缺失"]
