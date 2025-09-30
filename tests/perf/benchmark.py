from __future__ import annotations

import asyncio
import math
import statistics
import time
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from src.ibbot import influx
from src.ibbot.influx import (
    InfluxPoint,
    build_order_point,
    build_quote_point,
    build_service_metric_point,
)


@dataclass
class BenchmarkThresholds:
    """Threshold definitions for the performance benchmark."""

    min_throughput: float = 2000.0
    max_avg_latency: float = 0.005  # seconds
    max_p95_latency: float = 0.02  # seconds


@dataclass
class BenchmarkResult:
    """Summary of a benchmark execution."""

    throughput: float
    avg_latency: float
    p95_latency: float
    duration: float
    total_points: int
    write_batches: int
    service_metric_points: list[InfluxPoint] = field(default_factory=list)
    thresholds: BenchmarkThresholds = field(default_factory=BenchmarkThresholds)

    @property
    def passed(self) -> bool:
        return (
            self.throughput >= self.thresholds.min_throughput
            and self.avg_latency <= self.thresholds.max_avg_latency
            and self.p95_latency <= self.thresholds.max_p95_latency
        )


@dataclass
class _Event:
    kind: str
    enqueued_at: float
    sequence: int


class _FakeInfluxWriteAPI:
    def __init__(self, sink: "_FakeInfluxSink") -> None:
        self._sink = sink

    def write(self, *, bucket: str, org: str, record: Iterable[dict], write_precision: str) -> None:
        self._sink.write(bucket=bucket, org=org, record=list(record), write_precision=write_precision)


class _FakeInfluxClient:
    def __init__(self, sink: "_FakeInfluxSink") -> None:
        self._sink = sink

    def write_api(self, *, write_options: object | None = None) -> _FakeInfluxWriteAPI:
        return _FakeInfluxWriteAPI(self._sink)


class _FakeInfluxSink:
    def __init__(self) -> None:
        self.points: list[dict] = []
        self.write_calls = 0
        self.buckets: list[str] = []

    def write(self, *, bucket: str, org: str, record: list[dict], write_precision: str) -> None:
        self.write_calls += 1
        self.buckets.append(bucket)
        self.points.extend(record)


def _percentile(latencies: Sequence[float], percentile: float) -> float:
    if not latencies:
        return 0.0
    ordered = sorted(latencies)
    k = (len(ordered) - 1) * percentile
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return ordered[int(k)]
    return ordered[f] + (ordered[c] - ordered[f]) * (k - f)


async def _produce_events(queue: asyncio.Queue[_Event], *, total: int, consumer_count: int) -> None:
    for index in range(total):
        kind = "quote" if index % 3 else "order"
        event = _Event(kind=kind, enqueued_at=time.perf_counter(), sequence=index)
        await queue.put(event)
        if index % 250 == 0:
            await asyncio.sleep(0)
    # Signal completion to consumers
    for _ in range(consumer_count):
        await queue.put(_Event(kind="stop", enqueued_at=time.perf_counter(), sequence=-1))


async def _consume_events(
    queue: asyncio.Queue[_Event],
    *,
    sink: _FakeInfluxSink,
    batch_size: int,
    latencies: list[float],
    total_events: int,
) -> None:
    client = _FakeInfluxClient(sink)
    batch: list[InfluxPoint] = []
    processed = 0
    while processed < total_events:
        event = await queue.get()
        if event.kind == "stop":
            queue.task_done()
            break
        now = time.perf_counter()
        latencies.append(now - event.enqueued_at)
        if event.kind == "quote":
            point = build_quote_point(
                symbol="SYM", bid=100.0 + (event.sequence % 50) * 0.01,
                ask=100.1 + (event.sequence % 50) * 0.01,
                bid_size=10 + (event.sequence % 5),
                ask_size=10 + (event.sequence % 5),
                timestamp=None,
            )
        else:
            quantity = 10 + (event.sequence % 10)
            point = build_order_point(
                order_id=f"order-{event.sequence}",
                symbol="SYM",
                side="BUY" if event.sequence % 2 == 0 else "SELL",
                quantity=quantity,
                price=100.0 + (event.sequence % 50) * 0.02,
                status="filled" if event.sequence % 5 else "submitted",
            )
        batch.append(point)
        processed += 1
        queue.task_done()
        if len(batch) >= batch_size:
            influx.write_points(batch, bucket="benchmarks", org="bench", client=client, write_precision="ms")
            batch.clear()
    if batch:
        influx.write_points(batch, bucket="benchmarks", org="bench", client=client, write_precision="ms")


async def _run_async(
    *,
    event_count: int,
    concurrency: int,
    batch_size: int,
    thresholds: BenchmarkThresholds,
) -> BenchmarkResult:
    influx.reset_influx_state()
    influx.configure_influx(org="bench", bucket="benchmarks")
    influx.reset_write_statistics()

    queue: asyncio.Queue[_Event] = asyncio.Queue(maxsize=batch_size * concurrency)

    latencies: list[float] = []
    sink = _FakeInfluxSink()

    start_time = time.perf_counter()
    producer_task = asyncio.create_task(
        _produce_events(queue, total=event_count, consumer_count=concurrency)
    )
    consumer_tasks = [
        asyncio.create_task(
            _consume_events(
                queue,
                sink=sink,
                batch_size=batch_size,
                latencies=latencies,
                total_events=event_count,
            )
        )
        for _ in range(concurrency)
    ]

    await asyncio.gather(producer_task, *consumer_tasks)
    duration = time.perf_counter() - start_time

    throughput = event_count / duration if duration else float("inf")
    avg_latency = statistics.fmean(latencies) if latencies else 0.0
    p95_latency = _percentile(latencies, 0.95)
    stats = influx.get_write_statistics()

    status = "pass" if (
        throughput >= thresholds.min_throughput
        and avg_latency <= thresholds.max_avg_latency
        and p95_latency <= thresholds.max_p95_latency
    ) else "fail"

    service_metric_points = [
        build_service_metric_point(
            service="perf-benchmark",
            metric="throughput",
            value=throughput,
            status=status,
            extra_fields={"duration": duration, "points": event_count},
        ),
        build_service_metric_point(
            service="perf-benchmark",
            metric="latency",
            value=avg_latency,
            status=status,
            extra_fields={"p95": p95_latency},
        ),
    ]

    return BenchmarkResult(
        throughput=throughput,
        avg_latency=avg_latency,
        p95_latency=p95_latency,
        duration=duration,
        total_points=len(sink.points),
        write_batches=stats.batches,
        service_metric_points=service_metric_points,
        thresholds=thresholds,
    )


def run_benchmark(
    *,
    event_count: int = 5000,
    concurrency: int = 4,
    batch_size: int = 200,
    thresholds: BenchmarkThresholds | None = None,
) -> BenchmarkResult:
    """Execute the async benchmark in a synchronous context."""

    resolved_thresholds = thresholds or BenchmarkThresholds()
    result = asyncio.run(
        _run_async(
            event_count=event_count,
            concurrency=concurrency,
            batch_size=batch_size,
            thresholds=resolved_thresholds,
        )
    )
    return result


__all__ = ["run_benchmark", "BenchmarkResult", "BenchmarkThresholds"]
