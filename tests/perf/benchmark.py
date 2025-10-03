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
class WriteStatistics:
    batches: int = 0
    points: int = 0
    last_error: str | None = None

    def reset(self) -> None:
        self.batches = 0
        self.points = 0
        self.last_error = None

    def record_success(self, points: int) -> None:
        self.batches += 1
        self.points += points
        self.last_error = None

    def record_failure(self, error: Exception) -> None:
        self.batches += 1
        self.last_error = str(error)


WRITE_STATS = WriteStatistics()


@dataclass
class BenchmarkResult:
    """Summary of a benchmark execution."""

    throughput: float
    avg_latency: float
    p95_latency: float
    duration: float
    total_points: int
    write_batches: int
    service_metric_points: list[dict[str, Any]] = field(default_factory=list)
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


class _FakeSink:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.write_calls = 0

    def write(self, records: Iterable[dict[str, Any]]) -> None:
        batch = list(records)
        try:
            self.records.extend(batch)
        except Exception as exc:  # pragma: no cover - defensive
            WRITE_STATS.record_failure(exc)
            raise
        else:
            self.write_calls += 1
            WRITE_STATS.record_success(len(batch))


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


def _build_order_record(event: _Event, quantity: float, price: float) -> dict[str, Any]:
    return {
        "measurement": "orders",
        "fields": {
            "price": float(price),
            "quantity": float(quantity),
            "status": "filled" if event.sequence % 5 else "submitted",
        },
        "tags": {
            "order_id": f"order-{event.sequence}",
            "symbol": "SYM",
            "side": "BUY" if event.sequence % 2 == 0 else "SELL",
        },
    }


def _build_quote_record(event: _Event) -> dict[str, Any]:
    return {
        "measurement": "quotes",
        "fields": {
            "bid": 100.0 + (event.sequence % 50) * 0.01,
            "ask": 100.1 + (event.sequence % 50) * 0.01,
            "bid_size": 10 + (event.sequence % 5),
            "ask_size": 10 + (event.sequence % 5),
        },
        "tags": {"symbol": "SYM"},
    }


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
    sink: _FakeSink,
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
        if event.kind == "quote":
            record = _build_quote_record(event)
        else:
            quantity = 10 + (event.sequence % 10)
            price = 100.0 + (event.sequence % 50) * 0.02
            record = _build_order_record(event, quantity, price)
        batch.append(record)
        processed += 1
        queue.task_done()
        if len(batch) >= batch_size:
            sink.write(batch)
            batch.clear()
    if batch:
        sink.write(batch)


def _build_service_metrics(
    *,
    throughput: float,
    avg_latency: float,
    p95_latency: float,
    duration: float,
    event_count: int,
    status: str,
) -> list[dict[str, Any]]:
    return [
        {
            "service": "perf-benchmark",
            "metric": "throughput",
            "value": throughput,
            "status": status,
            "details": {"duration": duration, "points": event_count},
        },
        {
            "service": "perf-benchmark",
            "metric": "latency",
            "value": avg_latency,
            "status": status,
            "details": {"p95": p95_latency},
        },
    ]


async def _run_async(
    *,
    event_count: int,
    concurrency: int,
    batch_size: int,
    thresholds: BenchmarkThresholds,
) -> BenchmarkResult:
    WRITE_STATS.reset()

    queue: asyncio.Queue[_Event] = asyncio.Queue(maxsize=batch_size * concurrency)
    latencies: list[float] = []
    sink = _FakeSink()

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

    service_metric_points = _build_service_metrics(
        throughput=throughput,
        avg_latency=avg_latency,
        p95_latency=p95_latency,
        duration=duration,
        event_count=event_count,
        status=status,
    )

    return BenchmarkResult(
        throughput=throughput,
        avg_latency=avg_latency,
        p95_latency=p95_latency,
        duration=duration,
        total_points=len(sink.records),
        write_batches=WRITE_STATS.batches,
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


__all__ = ["run_benchmark", "BenchmarkResult", "BenchmarkThresholds", "WriteStatistics"]
