"""Tests for the optimizer service orchestration layer."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.optimizer.main import (
    EVENT_STREAM_KEY,
    RESULT_KEY_TEMPLATE,
    collect_dom_coverage,
    OptimizerDataError,
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


def test_optimizer_dom_coverage_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app()
    client = TestClient(app)

    captured: dict[str, Any] = {}

    def fake_describe(symbol: str, *, limit: int | None = None) -> dict[str, Any]:
        captured["symbol"] = symbol
        captured["limit"] = limit
        return {
            "symbol": symbol.upper(),
            "storage_root": f"/data/dom/{symbol.upper()}",
            "file_count": 2,
            "displayed_files": 1,
            "total_rows": 42,
            "earliest_ns": 1,
            "latest_ns": 2,
            "earliest_iso": "1970-01-01T00:00:00+00:00",
            "latest_iso": "1970-01-01T00:00:00+00:00",
            "files": [
                {
                    "file": "segment.parquet",
                    "path": "/data/dom/segment.parquet",
                    "size": 1024,
                    "rows": 42,
                    "start_ns": 1,
                    "end_ns": 2,
                    "modified_ns": 3,
                    "start_iso": "1970-01-01T00:00:00+00:00",
                    "end_iso": "1970-01-01T00:00:00+00:00",
                    "modified_iso": "1970-01-01T00:00:03+00:00",
                }
            ],
        }

    monkeypatch.setattr(
        "src.optimizer_service.routes.describe_dom_storage",
        fake_describe,
    )

    response = client.get(
        "/api/optimize/dom/coverage",
        params={"symbol": "es", "limit": 3},
    )
    assert response.status_code == 200
    payload = response.json()
    assert captured["symbol"] == "es"
    assert captured["limit"] == 3
    assert payload["symbol"] == "ES"
    assert payload["files"][0]["file"] == "segment.parquet"

    response_all = client.get(
        "/api/optimize/dom/coverage",
        params={"symbol": "es", "limit": 0},
    )
    assert response_all.status_code == 200
    assert captured["limit"] is None

    def failing_describe(*_: Any, **__: Any) -> dict[str, Any]:
        raise RuntimeError("offline")

    monkeypatch.setattr(
        "src.optimizer_service.routes.describe_dom_storage",
        failing_describe,
    )

    error_response = client.get("/api/optimize/dom/coverage", params={"symbol": "es"})
    assert error_response.status_code == 503
    assert "offline" in error_response.json()["detail"]


def test_collect_dom_coverage_resolves_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, int | None]] = []

    monkeypatch.setattr(
        "services.optimizer.main.list_dom_symbols",
        lambda: ["ES", "NQ"],
    )

    def fake_describe(symbol: str, *, limit: int | None = None) -> dict[str, Any]:
        seen.append((symbol, limit))
        return {
            "symbol": symbol,
            "storage_root": f"/data/dom/{symbol}",
            "file_count": 0,
            "displayed_files": 0,
            "total_rows": 0,
            "earliest_ns": None,
            "latest_ns": None,
            "earliest_iso": None,
            "latest_iso": None,
            "files": [],
        }

    monkeypatch.setattr(
        "services.optimizer.main.describe_dom_storage",
        fake_describe,
    )

    result = collect_dom_coverage(["es", "nq", "es"], limit=0)
    assert [entry["symbol"] for entry in result] == ["ES", "NQ"]
    assert seen[0][0] == "ES"
    assert seen[0][1] is None

    result_all = collect_dom_coverage(None, limit=2)
    assert [entry["symbol"] for entry in result_all] == ["ES", "NQ"]
    assert seen[-1][1] == 2

    def fail_list() -> list[str]:
        raise RuntimeError("unmounted")

    monkeypatch.setattr("services.optimizer.main.list_dom_symbols", fail_list)

    with pytest.raises(OptimizerDataError):
        collect_dom_coverage(None, limit=1)


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


def test_dom_strategy_requires_dom_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = create_fake_redis()

    async def scenario() -> None:
        await redis.hset(
            "hash:optimizer:strategy:mnq_dom:config",
            mapping={
                "name": json.dumps("mnq_dom"),
                "symbol": json.dumps("MNQ"),
                "requires_dom": json.dumps(True),
                "start_date": json.dumps("2023-01-01"),
                "end_date": json.dumps("2023-01-03"),
            },
        )
        await redis.hset(
            "hash:optimizer:strategy:mnq_dom:params",
            mapping={"window": json.dumps([5]), "threshold": json.dumps([0.1])},
        )
        await redis.hset(
            "hash:optimizer:strategy:mnq_dom:history",
            mapping={"sharpe": json.dumps(1.0)},
        )

        class DummyWriter:
            def __init__(self) -> None:
                self.base_dir = Path("/tmp/dom")
                self._reason = None

            def read_dom(self, symbol: str, *, limit: int | None = None, **_: Any) -> list[dict[str, Any]]:
                return []

        summary = SimpleNamespace(total_rows=0, earliest_ns=None, latest_ns=None, files=[])

        import services.optimizer.main as optimizer_main

        monkeypatch.setattr(optimizer_main, "get_market_data_writer", lambda: DummyWriter())
        monkeypatch.setattr(
            optimizer_main,
            "summarise_dom_directory",
            lambda base_dir, symbol: summary,
        )

        async def db_query(_: str) -> list[dict[str, Any]]:
            return [{"value": 0.5}]

        service = OptimizerService(redis_factory=lambda: redis, db_query=db_query)

        submission = TaskSubmission(
            task_id="dom-missing",
            strategy_id="mnq_dom",
            training_query="parquet://dom",
        )

        with pytest.raises(OptimizerDataError) as excinfo:
            await service.submit_task(submission)

        assert "backfill" in str(excinfo.value).lower()

    asyncio.run(scenario())


def test_dom_strategy_validates_dom_coverage(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = create_fake_redis()

    async def scenario() -> None:
        await redis.hset(
            "hash:optimizer:strategy:mnq_dom:config",
            mapping={
                "name": json.dumps("mnq_dom"),
                "symbol": json.dumps("MNQ"),
                "requires_dom": json.dumps(True),
                "start_date": json.dumps("2023-01-01"),
                "end_date": json.dumps("2023-01-03"),
            },
        )
        await redis.hset(
            "hash:optimizer:strategy:mnq_dom:params",
            mapping={"window": json.dumps([5]), "threshold": json.dumps([0.1])},
        )
        await redis.hset(
            "hash:optimizer:strategy:mnq_dom:history",
            mapping={"sharpe": json.dumps(1.0)},
        )

        base_time = datetime(2023, 1, 1, tzinfo=timezone.utc)
        summary = SimpleNamespace(
            total_rows=100,
            earliest_ns=int((base_time - timedelta(days=1)).timestamp() * 1_000_000_000),
            latest_ns=int((base_time + timedelta(days=5)).timestamp() * 1_000_000_000),
            files=[],
        )

        class DummyWriter:
            def __init__(self) -> None:
                self.base_dir = Path("/tmp/dom")
                self._reason = None

            def read_dom(self, symbol: str, *, limit: int | None = None, **_: Any) -> list[dict[str, Any]]:
                return [
                    {
                        "symbol": symbol,
                        "timestamp": "2023-01-01T00:00:00+00:00",
                        "bids": [],
                        "asks": [],
                    }
                ]

        import services.optimizer.main as optimizer_main

        monkeypatch.setattr(optimizer_main, "get_market_data_writer", lambda: DummyWriter())
        monkeypatch.setattr(
            optimizer_main,
            "summarise_dom_directory",
            lambda base_dir, symbol: summary,
        )

        async def db_query(_: str) -> list[dict[str, Any]]:
            return [{"value": 0.4}, {"value": 0.6}]

        service = OptimizerService(redis_factory=lambda: redis, db_query=db_query)

        submission = TaskSubmission(
            task_id="dom-ok",
            strategy_id="mnq_dom",
            training_query="parquet://dom",
        )

        result = await service.submit_task(submission)
        assert result.status == "completed"

    asyncio.run(scenario())


def test_dom_strategy_rejects_insufficient_range(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = create_fake_redis()

    async def scenario() -> None:
        await redis.hset(
            "hash:optimizer:strategy:mnq_dom:config",
            mapping={
                "name": json.dumps("mnq_dom"),
                "symbol": json.dumps("MNQ"),
                "requires_dom": json.dumps(True),
                "start_date": json.dumps("2023-01-01"),
                "end_date": json.dumps("2023-01-05"),
            },
        )
        await redis.hset(
            "hash:optimizer:strategy:mnq_dom:params",
            mapping={"window": json.dumps([5]), "threshold": json.dumps([0.1])},
        )
        await redis.hset(
            "hash:optimizer:strategy:mnq_dom:history",
            mapping={"sharpe": json.dumps(1.0)},
        )

        base_time = datetime(2023, 1, 1, tzinfo=timezone.utc)
        summary = SimpleNamespace(
            total_rows=50,
            earliest_ns=int(base_time.timestamp() * 1_000_000_000),
            latest_ns=int((base_time + timedelta(days=1)).timestamp() * 1_000_000_000),
            files=[],
        )

        class DummyWriter:
            def __init__(self) -> None:
                self.base_dir = Path("/tmp/dom")
                self._reason = None

            def read_dom(self, symbol: str, *, limit: int | None = None, **_: Any) -> list[dict[str, Any]]:
                return [
                    {
                        "symbol": symbol,
                        "timestamp": "2023-01-02T00:00:00+00:00",
                        "bids": [],
                        "asks": [],
                    }
                ]

        import services.optimizer.main as optimizer_main

        monkeypatch.setattr(optimizer_main, "get_market_data_writer", lambda: DummyWriter())
        monkeypatch.setattr(
            optimizer_main,
            "summarise_dom_directory",
            lambda base_dir, symbol: summary,
        )

        async def db_query(_: str) -> list[dict[str, Any]]:
            return [{"value": 0.5}]

        service = OptimizerService(redis_factory=lambda: redis, db_query=db_query)

        submission = TaskSubmission(
            task_id="dom-range",
            strategy_id="mnq_dom",
            training_query="parquet://dom",
        )

        with pytest.raises(OptimizerDataError) as excinfo:
            await service.submit_task(submission)

        message = str(excinfo.value)
        assert "ends on" in message

    asyncio.run(scenario())
