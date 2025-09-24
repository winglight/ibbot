import os

os.environ.setdefault("IBBOT_SKIP_SERVER", "1")

from datetime import date

from src.optimizer_service.schemas import OptimizationAlgorithm, OptimizationRequest
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
