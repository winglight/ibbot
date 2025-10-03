from __future__ import annotations

from tests.perf.benchmark import BenchmarkThresholds, run_benchmark


def test_performance_benchmark_meets_thresholds() -> None:
    thresholds = BenchmarkThresholds(
        min_throughput=1800.0,
        max_avg_latency=0.01,
        max_p95_latency=0.03,
    )
    result = run_benchmark(event_count=4000, concurrency=4, batch_size=200, thresholds=thresholds)

    assert result.passed, (
        "Performance benchmark did not meet thresholds: "
        f"throughput={result.throughput:.2f}, avg_latency={result.avg_latency:.6f}, "
        f"p95_latency={result.p95_latency:.6f}"
    )
    assert result.total_events == 4000
    assert result.persistence_batches > 0
    assert result.persistence_duration >= 0.0

    metrics = {sample.metric: sample for sample in result.service_metric_samples}
    assert set(metrics) == {"throughput", "latency", "persistence_latency"}
    assert metrics["latency"].details["p95"] == result.p95_latency
    assert metrics["throughput"].value >= thresholds.min_throughput
    assert metrics["persistence_latency"].details["batches"] == result.persistence_batches
