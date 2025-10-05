"""Tests for the optimizer service orchestration layer."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.optimizer.main import (
    EVENT_STREAM_KEY,
    RESULT_KEY_TEMPLATE,
    OptimizerService,
    TaskSubmission,
    create_app,
)

try:  # pragma: no cover - prefer fakeredis when available
    import fakeredis.aioredis as fakeredis  # type: ignore
except Exception:  # pragma: no cover - fallback to local stub
    fakeredis = None  # type: ignore


class _InMemoryRedis:
    """Minimal async Redis stub for tests when fakeredis is unavailable."""

    def __init__(self) -> None:
        self._hashes: dict[str, dict[str, str]] = {}
        self._streams: dict[str, list[tuple[str, dict[str, str]]]] = {}

    async def hset(self, key: str, *, mapping: dict[str, Any]) -> int:
        store = self._hashes.setdefault(key, {})
        for field, value in mapping.items():
            store[field] = str(value)
        return len(mapping)

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self._hashes.get(key, {}))

    async def xadd(self, key: str, mapping: dict[str, Any]) -> str:
        entries = self._streams.setdefault(key, [])
        entry_id = f"{len(entries)+1}-0"
        entries.append((entry_id, {k: str(v) for k, v in mapping.items()}))
        return entry_id

    async def xrange(self, key: str, min: str = "-", max: str = "+") -> list[tuple[str, dict[str, str]]]:
        return list(self._streams.get(key, []))


def create_fake_redis() -> Any:
    if fakeredis is not None:
        return fakeredis.FakeRedis(decode_responses=True)  # type: ignore[attr-defined]
    return _InMemoryRedis()


def test_submit_task_records_progress_and_result() -> None:
    redis = create_fake_redis()

    async def scenario() -> None:
        async def db_query(_: str) -> list[dict[str, Any]]:
            return [{"value": 0.5}, {"value": 0.75}]

        service = OptimizerService(redis_factory=lambda: redis, db_query=db_query)

        await redis.hset(
            "hash:optimizer:strategy:demo:config",
            mapping={"name": json.dumps("demo"), "risk": json.dumps("medium")},
        )
        await redis.hset(
            "hash:optimizer:strategy:demo:params",
            mapping={
                "window": json.dumps([5, 10, 15]),
                "threshold": json.dumps([0.1, 0.2]),
            },
        )
        await redis.hset(
            "hash:optimizer:strategy:demo:history",
            mapping={"sharpe": json.dumps(1.2)},
        )

        submission = TaskSubmission(
            task_id="task-1",
            strategy_id="demo",
            training_query="parquet://market-data/candles",
        )

        result = await service.submit_task(submission)

        assert result.status == "completed"
        assert result.best_params == {"window": 5, "threshold": 0.1}
        assert result.metrics["baseline_sharpe"] == pytest.approx(1.2)
        assert result.metrics["score"] == pytest.approx(0.625)
        assert result.samples == 2

        events = await redis.xrange(EVENT_STREAM_KEY, min="-", max="+")
        assert [entry[1]["progress"] for entry in events] == ["0", "50", "100"]

        stored_result = await redis.hgetall(RESULT_KEY_TEMPLATE.format(task_id="task-1"))
        assert json.loads(stored_result["best_params"]) == {"window": 5, "threshold": 0.1}
        metrics = json.loads(stored_result["metrics"])
        assert metrics["baseline_sharpe"] == pytest.approx(1.2)
        assert metrics["score"] == pytest.approx(0.625)

    asyncio.run(scenario())


def test_get_result_returns_parsed_payload() -> None:
    redis = create_fake_redis()

    async def scenario() -> None:
        await redis.hset(
            RESULT_KEY_TEMPLATE.format(task_id="task-2"),
            mapping={
                "status": "completed",
                "best_params": json.dumps({"window": 10}),
                "metrics": json.dumps({"score": 0.8, "baseline_sharpe": 1.1}),
                "samples": "3",
            },
        )

        service = OptimizerService(redis_factory=lambda: redis, db_query=lambda _: [])

        result = await service.get_result("task-2")
        assert result.task_id == "task-2"
        assert result.best_params == {"window": 10}
        assert result.metrics == {"score": 0.8, "baseline_sharpe": 1.1}
        assert result.samples == 3

    asyncio.run(scenario())


def test_fastapi_endpoints_submit_and_read_results() -> None:
    redis = create_fake_redis()

    async def setup() -> None:
        await redis.hset(
            "hash:optimizer:strategy:api:config",
            mapping={"name": json.dumps("api"), "risk": json.dumps("low")},
        )
        await redis.hset(
            "hash:optimizer:strategy:api:params",
            mapping={"window": json.dumps([3, 6]), "threshold": json.dumps([0.05, 0.1])},
        )
        await redis.hset(
            "hash:optimizer:strategy:api:history",
            mapping={"sharpe": json.dumps(0.9)},
        )

    asyncio.run(setup())

    async def db_query(_: str) -> list[dict[str, Any]]:
        return [{"value": 0.4}, {"value": 0.6}, {"value": 0.8}]

    service = OptimizerService(redis_factory=lambda: redis, db_query=db_query)
    app = create_app(service=service)

    client = TestClient(app)
    response = client.post(
        "/api/optimizer/tasks",
        json={
            "task_id": "task-api",
            "strategy_id": "api",
            "training_query": "parquet://market-data/candles",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"task_id": "task-api", "status": "accepted"}

    response = client.get("/api/optimizer/tasks/task-api/result")
    assert response.status_code == 200
    payload = response.json()
    assert payload["best_params"] == {"window": 3, "threshold": 0.05}
    assert payload["samples"] == 3
    assert pytest.approx(payload["metrics"]["score"], rel=1e-6) == 0.6
