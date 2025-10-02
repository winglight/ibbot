import asyncio
import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.routes import queue as queue_module
from src.routes.queue_monitor import QueueMonitorClient


@pytest.fixture()
def queue_app() -> FastAPI:
    app = FastAPI()
    app.include_router(queue_module.router)
    yield app
    queue_module.set_queue_monitor_client(None)


class _RedisStub:
    def __init__(self) -> None:
        self._hashes: dict[str, dict[str, Any]] = {}
        self._lists: dict[str, list[Any]] = {}

    async def flushdb(self) -> None:
        self._hashes.clear()
        self._lists.clear()

    async def rpush(self, key: str, *values: Any) -> int:
        items = self._lists.setdefault(key, [])
        items.extend(values)
        return len(items)

    async def lpush(self, key: str, *values: Any) -> int:
        items = self._lists.setdefault(key, [])
        for value in reversed(values):
            items.insert(0, value)
        return len(items)

    async def llen(self, key: str) -> int:
        return len(self._lists.get(key, []))

    async def hset(self, key: str, mapping: dict[str, Any]) -> int:
        bucket = self._hashes.setdefault(key, {})
        bucket.update(mapping)
        return len(mapping)

    async def hgetall(self, key: str) -> dict[str, Any]:
        return dict(self._hashes.get(key, {}))


def _make_redis_client() -> Any:
    try:
        import fakeredis.aioredis as fakeredis  # type: ignore
    except Exception:
        return _RedisStub()
    return fakeredis.FakeRedis(decode_responses=True)  # type: ignore[attr-defined]


def _create_client(redis_client: Any) -> QueueMonitorClient:
    async def _factory() -> Any:
        return redis_client

    return QueueMonitorClient(redis_factory=_factory)


def test_queue_status_returns_snapshot(queue_app: FastAPI) -> None:
    redis_client = _make_redis_client()
    loop = asyncio.get_event_loop()
    loop.run_until_complete(redis_client.flushdb())
    loop.run_until_complete(redis_client.rpush("queue:ib:commands", "a", "b"))
    snapshot = {
        "timestamp": "2024-01-01T00:00:00+00:00",
        "queue:ib-commands": json.dumps(
            {
                "name": "ib-commands",
                "queue_key": "queue:ib:commands",
                "length": 5,
                "consumption_rate": 1.5,
                "stale_seconds": 0,
            }
        ),
        "stream:orders": json.dumps(
            {
                "name": "orders",
                "stream_key": "stream:orders",
                "group": "orders-group",
                "pending": 7,
                "consumption_rate": 2.5,
                "stale_seconds": 10,
            }
        ),
    }
    loop.run_until_complete(redis_client.hset("hash:service:queue-monitor", mapping=snapshot))

    queue_module.set_queue_monitor_client(_create_client(redis_client))

    with TestClient(queue_app) as client:
        status_response = client.get("/api/queue/status")

    assert status_response.status_code == 200
    body = status_response.json()
    assert body["timestamp"] == "2024-01-01T00:00:00+00:00"
    assert body["totals"]["queue_length"] == 2
    assert body["totals"]["stream_pending"] == 7
    assert body["queues"]
    queue_entry = body["queues"][0]
    assert queue_entry["name"] == "ib-commands"
    assert queue_entry["queue_key"] == "queue:ib:commands"
    assert queue_entry["length"] == 2
    assert queue_entry["snapshot_length"] == 5
    assert body["streams"]
    stream_entry = body["streams"][0]
    assert stream_entry["name"] == "orders"
    assert stream_entry["pending"] == 7


def test_queue_tasks_and_single_queue(queue_app: FastAPI) -> None:
    redis_client = _make_redis_client()
    loop = asyncio.get_event_loop()
    loop.run_until_complete(redis_client.flushdb())
    loop.run_until_complete(redis_client.lpush("queue:ib:commands", "x"))
    snapshot = {
        "timestamp": "2024-02-02T00:00:00+00:00",
        "queue:ib-commands": json.dumps(
            {
                "name": "ib-commands",
                "queue_key": "queue:ib:commands",
                "length": 4,
                "consumption_rate": 0.2,
                "stale_seconds": 5,
            }
        ),
    }
    loop.run_until_complete(redis_client.hset("hash:service:queue-monitor", mapping=snapshot))

    queue_module.set_queue_monitor_client(_create_client(redis_client))

    with TestClient(queue_app) as client:
        tasks_response = client.get("/api/queue/tasks")
        queue_response = client.get("/api/queue/task/ib-commands")
        queue_by_key = client.get("/api/queue/task/queue:ib:commands")
        missing_queue = client.get("/api/queue/task/other")

    assert tasks_response.status_code == 200
    tasks_body = tasks_response.json()
    assert tasks_body["timestamp"] == "2024-02-02T00:00:00+00:00"
    assert tasks_body["queues"]
    assert tasks_body["queues"][0]["length"] == 1

    assert queue_response.status_code == 200
    queue_body = queue_response.json()
    assert queue_body["timestamp"] == "2024-02-02T00:00:00+00:00"
    assert queue_body["queue"]["name"] == "ib-commands"
    assert queue_body["queue"]["length"] == 1

    assert queue_by_key.status_code == 200
    assert queue_by_key.json()["queue"]["queue_key"] == "queue:ib:commands"

    assert missing_queue.status_code == 404
    assert missing_queue.json()["detail"] == "Queue not found"


def test_queue_routes_when_snapshot_missing(queue_app: FastAPI) -> None:
    redis_client = _make_redis_client()
    loop = asyncio.get_event_loop()
    loop.run_until_complete(redis_client.flushdb())

    queue_module.set_queue_monitor_client(_create_client(redis_client))

    with TestClient(queue_app) as client:
        status_response = client.get("/api/queue/status")
        tasks_response = client.get("/api/queue/tasks")
        queue_response = client.get("/api/queue/task/ib-commands")

    assert status_response.status_code == 503
    assert status_response.json()["detail"] == "Queue monitor snapshot unavailable"

    assert tasks_response.status_code == 200
    tasks_body = tasks_response.json()
    assert tasks_body["queues"] == []
    assert tasks_body["timestamp"] is None
    assert tasks_body["detail"] == "Queue monitor snapshot unavailable"

    assert queue_response.status_code == 503
    assert queue_response.json()["detail"] == "Queue monitor snapshot unavailable"
