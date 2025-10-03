#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.perf.benchmark import BenchmarkThresholds, run_benchmark  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the ibbot performance benchmark")
    parser.add_argument("--events", type=int, default=5000, help="Number of synthetic events to process")
    parser.add_argument("--concurrency", type=int, default=4, help="Number of consumer tasks")
    parser.add_argument("--batch-size", type=int, default=200, help="Number of points per synthetic write batch")
    parser.add_argument("--min-throughput", type=float, default=2000.0, help="Minimum acceptable throughput")
    parser.add_argument("--max-avg-latency", type=float, default=0.005, help="Maximum average latency in seconds")
    parser.add_argument("--max-p95-latency", type=float, default=0.02, help="Maximum p95 latency in seconds")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    thresholds = BenchmarkThresholds(
        min_throughput=args.min_throughput,
        max_avg_latency=args.max_avg_latency,
        max_p95_latency=args.max_p95_latency,
    )
    result = run_benchmark(
        event_count=args.events,
        concurrency=args.concurrency,
        batch_size=args.batch_size,
        thresholds=thresholds,
    )

    summary = {
        "throughput": result.throughput,
        "avg_latency": result.avg_latency,
        "p95_latency": result.p95_latency,
        "duration": result.duration,
        "total_points": result.total_points,
        "write_batches": result.write_batches,
        "thresholds": {
            "min_throughput": thresholds.min_throughput,
            "max_avg_latency": thresholds.max_avg_latency,
            "max_p95_latency": thresholds.max_p95_latency,
        },
        "service_metrics": result.service_metric_points,
        "status": "pass" if result.passed else "fail",
    }

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print("Performance benchmark summary:")
        print(f"  Throughput: {result.throughput:.2f} events/sec")
        print(f"  Avg latency: {result.avg_latency * 1000:.3f} ms")
        print(f"  P95 latency: {result.p95_latency * 1000:.3f} ms")
        print(f"  Duration: {result.duration:.3f} s for {result.total_points} points")
        print(f"  Synthetic write batches: {result.write_batches}")
        print(f"  Status: {'PASS' if result.passed else 'FAIL'}")

    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
