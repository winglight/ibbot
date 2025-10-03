from __future__ import annotations

import asyncio
import json
from typing import Any, Iterable, Mapping

from services.queue_monitor import QueueMonitorRuntime, QueueTarget, StreamTarget


class FakeRedis:
    def __init__(self) -> None:
        self.queue_lengths: dict[str, int] = {}
        self.stream_state: dict[tuple[str, str], dict[str, Any]] = {}
        self.hash_store: dict[str, dict[str, str]] = {}
        self.published: list[tuple[str, str]] = []

    async def llen(self, key: str) -> int:
        return self.queue_lengths.get(key, 0)

    async def hset(self, name: str, *, mapping: Mapping[str, str]) -> int:
        store = self.hash_store.setdefault(name, {})
        store.update({str(k): str(v) for k, v in mapping.items()})
        return len(mapping)

    async def publish(self, channel: str, message: str) -> int:
        self.published.append((channel, message))
        return len(self.published)

    async def xpending(self, stream_key: str, group: str) -> Mapping[str, Any]:
        state = self.stream_state.get((stream_key, group))
        if not state:
            return {"pending": 0}
        return {"pending": state.get("pending", 0)}

    async def xpending_range(
        self,
        stream_key: str,
        group: str,
        min: str,
        max: str,
        count: int,
    ) -> list[Any]:
        state = self.stream_state.get((stream_key, group))
        if not state or state.get("details") is None:
            return []
        return [state["details"]]


class RecordingRepository:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.calls: list[list[dict[str, Any]]] = []
        self.closed = False

    async def record_metrics(self, metrics: Mapping[str, Any] | Iterable[Mapping[str, Any]]) -> None:
        if isinstance(metrics, Mapping):
            batch = [dict(metrics)]
        else:
            batch = [dict(entry) for entry in metrics]
        self.calls.append(batch)
        self.records.extend(batch)

    async def close(self) -> None:
        self.closed = True


def test_queue_monitor_aggregates_metrics() -> None:
    redis = FakeRedis()
    redis.queue_lengths["queue:ib:commands"] = 10
    redis.stream_state[("stream:jobs", "group:jobs")] = {
        "pending": 5,
        "details": ("id-1", "consumer-1", 1500, 1),
    }
    repository = RecordingRepository()

    async def redis_factory() -> FakeRedis:
        return redis

    runtime = QueueMonitorRuntime(
        queues=[QueueTarget(name="commands", queue_key="queue:ib:commands")],
        stream_targets=[StreamTarget(name="jobs", stream_key="stream:jobs", group="group:jobs")],
        redis_factory=redis_factory,
        metrics_interval=60.0,
        pubsub_channel=None,
        metrics_repository_factory=lambda: repository,
    )

    async def run_monitor() -> tuple[dict[str, Any], dict[str, dict[str, str]], list[dict[str, Any]]]:
        await runtime.start()
        try:
            await runtime.collect_once()
            redis.queue_lengths["queue:ib:commands"] = 7
            redis.stream_state[("stream:jobs", "group:jobs")] = {
                "pending": 3,
                "details": ("id-2", "consumer-1", 800, 1),
            }
            await asyncio.sleep(0.05)
            await runtime.collect_once()
            snapshot = runtime.get_snapshot()
            hash_copy = {key: dict(value) for key, value in redis.hash_store.items()}
            records = list(repository.records)
            return snapshot, hash_copy, records
        finally:
            await runtime.stop()

    snapshot, hash_state_all, recorded_points = asyncio.run(run_monitor())
    queues = {entry["queue_key"]: entry for entry in snapshot["queues"]}
    queue_metrics = queues["queue:ib:commands"]
    assert queue_metrics["length"] == 7
    assert queue_metrics["consumption_rate"] > 0
    assert queue_metrics["stale_seconds"] > 0

    streams = {entry["stream_key"]: entry for entry in snapshot["streams"]}
    stream_metrics = streams["stream:jobs"]
    assert stream_metrics["pending"] == 3
    assert stream_metrics["consumption_rate"] > 0
    assert stream_metrics["stale_seconds"] >= 0

    hash_state = hash_state_all["hash:service:queue-monitor"]
    assert "queue:commands" in hash_state
    payload = json.loads(hash_state["queue:commands"])
    assert payload["length"] == 7

    health_key = runtime.health_hash_key
    assert health_key == "hash:service:health:queue"
    assert health_key in hash_state_all
    health_payload = hash_state_all[health_key]
    assert health_payload.get("total_queue_length") == "7"
    assert health_payload.get("total_stream_pending") == "3"

    assert recorded_points, "Expected service metrics to be recorded"
    metric_names = {entry.get("metric") for entry in recorded_points}
    assert "commands-queue-length" in metric_names
    assert "commands-consumption-rate" in metric_names
    assert "jobs-pending" in metric_names
    queue_entries = [entry for entry in recorded_points if entry.get("metric") == "commands-queue-length"]
    assert queue_entries
    details = queue_entries[-1].get("details") or {}
    assert details.get("queue_key") == "queue:ib:commands"
    stream_entries = [entry for entry in recorded_points if entry.get("metric") == "jobs-pending"]
    assert stream_entries
    stream_details = stream_entries[-1].get("details") or {}
    assert stream_details.get("stream_key") == "stream:jobs"
    assert stream_details.get("group") == "group:jobs"
    assert repository.closed


def test_collect_once_writes_health_to_queue_hash() -> None:
    redis = FakeRedis()
    repository = RecordingRepository()

    async def redis_factory() -> FakeRedis:
        return redis

    runtime = QueueMonitorRuntime(
        queues=[QueueTarget(name="commands", queue_key="queue:ib:commands")],
        stream_targets=[StreamTarget(name="jobs", stream_key="stream:jobs", group="group:jobs")],
        redis_factory=redis_factory,
        metrics_interval=60.0,
        pubsub_channel=None,
        metrics_repository_factory=lambda: repository,
    )

    async def run_collect_once() -> None:
        await runtime.collect_once()

    asyncio.run(run_collect_once())

    assert "hash:service:health:queue" in redis.hash_store
    assert "hash:service:health:queue-monitor" not in redis.hash_store
