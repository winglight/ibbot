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
    assert result.total_points == 4000
    assert result.write_batches > 0

    metrics = {point["metric"]: point for point in result.service_metric_points}
    assert set(metrics) == {"throughput", "latency"}
    assert metrics["latency"]["details"]["p95"] == result.p95_latency
    assert metrics["throughput"]["value"] >= thresholds.min_throughput
