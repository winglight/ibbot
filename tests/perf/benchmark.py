from __future__ import annotations

import asyncio
import math
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence


@dataclass
class BenchmarkThresholds:
    """Threshold definitions for the performance benchmark."""

    min_throughput: float = 2000.0
    max_avg_latency: float = 0.005  # seconds
    max_p95_latency: float = 0.02  # seconds


@dataclass
class MetricSample:
    """Lightweight representation of a service metric sample."""

    service: str
    metric: str
    value: float
    status: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class BenchmarkResult:
    """Summary of a benchmark execution."""

    throughput: float
    avg_latency: float
    p95_latency: float
    duration: float
    total_events: int
    persistence_batches: int
    persistence_duration: float
    avg_persistence_latency: float
    service_metric_samples: list[MetricSample] = field(default_factory=list)
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


class _EventSink:
    """Collect batches of events for inspection by the benchmark."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.persistence_calls = 0
        self.total_persistence_time = 0.0

    def persist_batch(self, batch: Iterable[dict[str, Any]]) -> None:
        materialised = [dict(entry) for entry in batch]
        if not materialised:
            return
        start = time.perf_counter()
        self.persistence_calls += 1
        self.events.extend(materialised)
        self.total_persistence_time += time.perf_counter() - start


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
    for _ in range(consumer_count):
        await queue.put(_Event(kind="stop", enqueued_at=time.perf_counter(), sequence=-1))


async def _consume_events(
    queue: asyncio.Queue[_Event],
    *,
    sink: _EventSink,
    batch_size: int,
    latencies: list[float],
    total_events: int,
) -> None:
    batch: list[dict[str, Any]] = []
    processed = 0
    while processed < total_events:
        event = await queue.get()
        if event.kind == "stop":
            queue.task_done()
            break
        now = time.perf_counter()
        latencies.append(now - event.enqueued_at)
        payload = {
            "type": event.kind,
            "sequence": event.sequence,
            "processed_at": now,
        }
        batch.append(payload)
        processed += 1
        queue.task_done()
        if len(batch) >= batch_size:
            sink.persist_batch(batch)
            batch.clear()
    if batch:
        sink.persist_batch(batch)


async def _run_async(
    *,
    event_count: int,
    concurrency: int,
    batch_size: int,
    thresholds: BenchmarkThresholds,
) -> BenchmarkResult:
    queue: asyncio.Queue[_Event] = asyncio.Queue(maxsize=batch_size * max(1, concurrency))

    latencies: list[float] = []
    sink = _EventSink()

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

    status = "pass" if (
        throughput >= thresholds.min_throughput
        and avg_latency <= thresholds.max_avg_latency
        and p95_latency <= thresholds.max_p95_latency
    ) else "fail"

    metric_samples = [
        MetricSample(
            service="perf-benchmark",
            metric="throughput",
            value=throughput,
            status=status,
            details={"duration": duration, "events": event_count},
        ),
        MetricSample(
            service="perf-benchmark",
            metric="latency",
            value=avg_latency,
            status=status,
            details={"p95": p95_latency},
        ),
        MetricSample(
            service="perf-benchmark",
            metric="persistence_latency",
            value=(
                sink.total_persistence_time / sink.persistence_calls
                if sink.persistence_calls
                else 0.0
            ),
            status=status,
            details={
                "batches": sink.persistence_calls,
                "total_time": sink.total_persistence_time,
            },
        ),
    ]

    persistence_avg = (
        sink.total_persistence_time / sink.persistence_calls
        if sink.persistence_calls
        else 0.0
    )

    return BenchmarkResult(
        throughput=throughput,
        avg_latency=avg_latency,
        p95_latency=p95_latency,
        duration=duration,
        total_events=len(sink.events),
        persistence_batches=sink.persistence_calls,
        persistence_duration=sink.total_persistence_time,
        avg_persistence_latency=persistence_avg,
        service_metric_samples=metric_samples,
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


__all__ = ["run_benchmark", "BenchmarkResult", "BenchmarkThresholds", "MetricSample"]
