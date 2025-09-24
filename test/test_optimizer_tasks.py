import os

os.environ.setdefault("IBBOT_SKIP_SERVER", "1")

from datetime import date, datetime

from src.optimizer_service.db import OptimizerDatabase
from src.optimizer_service.schemas import (
    OptimizationAlgorithm,
    OptimizationReportData,
    OptimizationRequest,
    OptimizationResultItem,
)
from src.optimizer_service.storage import OptimizerTaskStore
from src.optimizer_service.tasks import run_optimization_task


class DummyStore:
    def __init__(self) -> None:
        self.states = []
        self.fail_messages = []
        self.completed_report = None
        self.failed = False
        self.fail_reasons = []

    def update_state(self, task_id, state, progress=None, message=None):  # noqa: D401 - simple proxy
        self.states.append((state, progress, message))

    def append_fail_reason(self, task_id, reason):
        self.fail_reasons.append(reason)

    def fail_task(self, task_id, message):
        self.failed = True
        self.fail_messages.append(message)

    def complete_task(self, task_id, report):
        self.completed_report = report


def make_request() -> OptimizationRequest:
    return OptimizationRequest(
        strategy="task_test",
        start_date=date(2023, 1, 1),
        end_date=date(2023, 1, 10),
        parameters={"window": [5, 10], "threshold": [0.1, 0.2]},
        algorithm=OptimizationAlgorithm.GRID,
    )


def test_run_optimization_task_produces_runtime_summary():
    store = DummyStore()
    request = make_request()

    run_optimization_task("task-123", request, store)

    assert store.failed is False
    assert store.completed_report is not None
    summary = store.completed_report.summary

    runtime = summary.get("runtime")
    assert runtime is not None
    assert runtime["actual_duration_seconds"] > 0
    assert runtime["expected_duration_seconds"] >= 0
    assert runtime["tests_per_second"] is not None
    assert runtime["actual_total_combinations"] == summary["tested_combinations"]

    distribution = summary.get("annual_return_distribution")
    assert distribution is not None
    assert distribution["min"] <= distribution["max"]
    for key in ("p25", "p50", "p75", "mean"):
        assert key in distribution

    conclusion = summary.get("conclusion")
    assert isinstance(conclusion, str)
    assert conclusion

    assert summary["successful_combinations"] >= 1


def test_legacy_report_summary_is_enriched():
    db = OptimizerDatabase(":memory:")
    store = OptimizerTaskStore(db)
    request = make_request()
    plan_id = "plan-1"
    task_id = "legacy-task"

    store.create_task(task_id, request, plan_id=plan_id)
    legacy_report = OptimizationReportData(
        task_id=task_id,
        results=[
            OptimizationResultItem(
                parameters={"window": 5},
                metrics={"sharpe": 1.25, "max_drawdown": 0.12, "annual_return": 0.2},
            ),
            OptimizationResultItem(
                parameters={"window": 10},
                metrics={"sharpe": 0.9, "max_drawdown": 0.18, "annual_return": 0.11},
            ),
        ],
        summary={
            "best_sharpe": 1.25,
            "best_max_drawdown": 0.12,
            "best_annual_return": 0.2,
            "tested_combinations": 2,
            "successful_combinations": 2,
            "failed_combinations": 0,
            "generated_at": datetime.utcnow().isoformat() + "Z",
        },
        equity_curve=[],
        fail_reasons=[],
    )
    store.complete_task(task_id, legacy_report)

    records = store.list_tasks_by_plan(plan_id)
    assert records
    _, task_record = records[0]
    enriched_summary = task_record.report.summary

    assert "runtime" in enriched_summary
    assert "annual_return_distribution" in enriched_summary
    assert "conclusion" in enriched_summary

    runtime = enriched_summary["runtime"]
    assert runtime["actual_total_combinations"] == enriched_summary["tested_combinations"]

    distribution = enriched_summary["annual_return_distribution"]
    assert distribution["p25"] <= distribution["p75"]
