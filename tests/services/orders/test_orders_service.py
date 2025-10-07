import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Mapping

import pytest
from httpx import ASGITransport, AsyncClient

from services.orders.main import OrderSubmission, OrdersRuntime, create_app
from tests.services.redis_utils import InMemoryRedis


class RecordingRepository:
    def __init__(self) -> None:
        self.insert_calls = 0
        self.update_calls = 0
        self.fetch_recent_calls = 0
        self.paginate_calls: list[dict[str, Any]] = []
        self.fetch_order_calls: list[str] = []
        self.closed = False
        self._rows: dict[int, dict[str, Any]] = {}
        self._next_id = 1

    async def insert_order(self, payload: Mapping[str, Any]) -> int:
        self.insert_calls += 1
        record = dict(payload)
        record_id = self._next_id
        self._next_id += 1
        record.setdefault("id", record_id)
        self._rows[record_id] = record
        return record_id

    async def update_order(self, payload: Mapping[str, Any]) -> None:
        self.update_calls += 1
        record = dict(payload)
        identifier = str(record.get("command_id") or record.get("id") or "").strip()
        if not identifier:
            raise ValueError("missing identifier")
        for existing in self._rows.values():
            candidates = {
                str(existing.get("command_id") or ""),
                str(existing.get("id") or ""),
                str(existing.get("order_id") or ""),
            }
            if identifier in candidates:
                existing.update(record)
                return
        raise ValueError("order not found")

    async def fetch_recent_orders(self, *, limit: int) -> list[Mapping[str, Any]]:
        self.fetch_recent_calls += 1
        records = sorted(
            (dict(record) for record in self._rows.values()),
            key=lambda item: item.get("updated_at") or item.get("timestamp") or "",
            reverse=True,
        )
        if limit >= 0:
            return records[:limit]
        return records

    async def paginate_orders(
        self,
        *,
        offset: int,
        limit: int,
        start_date: datetime | None = None,
    ) -> tuple[list[Mapping[str, Any]], int]:
        self.paginate_calls.append({"offset": offset, "limit": limit, "start_date": start_date})
        records = sorted(
            (dict(record) for record in self._rows.values()),
            key=lambda item: item.get("updated_at") or item.get("timestamp") or "",
            reverse=True,
        )
        if start_date is not None:
            filtered: list[dict[str, Any]] = []
            for record in records:
                updated = record.get("updated_at") or record.get("timestamp")
                if isinstance(updated, str):
                    try:
                        updated_dt = datetime.fromisoformat(updated)
                    except Exception:
                        filtered.append(record)
                        continue
                    if updated_dt >= start_date:
                        filtered.append(record)
                else:
                    filtered.append(record)
            records = filtered
        total = len(records)
        if limit >= 0:
            return records[offset : offset + limit], total
        return records[offset:], total

    async def fetch_order(self, identifier: str) -> Mapping[str, Any] | None:
        text = str(identifier or "").strip()
        if not text:
            return None
        self.fetch_order_calls.append(text)
        for record in self._rows.values():
            candidates = {
                str(record.get("command_id") or ""),
                str(record.get("order_id") or ""),
                str(record.get("id") or ""),
            }
            if text in candidates:
                return dict(record)
        return None

    async def close(self) -> None:
        self.closed = True


async def _create_runtime(
    *,
    order_repository_factory: Callable[[], Awaitable[Any] | Any] | None = None,
    repository: RecordingRepository | None = None,
    **runtime_kwargs: Any,
) -> tuple[OrdersRuntime, InMemoryRedis, RecordingRepository]:
    redis = InMemoryRedis()

    async def redis_factory() -> InMemoryRedis:
        return redis

    repo = repository or RecordingRepository()

    async def default_repo_factory() -> RecordingRepository:
        return repo

    kwargs: dict[str, Any] = {
        "redis_factory": redis_factory,
        "order_repository_factory": order_repository_factory or default_repo_factory,
        "stream_poll_interval": 0.0,
        "health_interval": 0.0,
    }
    kwargs.update(runtime_kwargs)
    runtime = OrdersRuntime(**kwargs)
    await runtime.start()
    return runtime, redis, repo


@pytest.mark.asyncio
async def test_runtime_start_hydrates_from_repository() -> None:
    repository = RecordingRepository()
    now = datetime.now(timezone.utc)
    repository._rows = {
        1: {
            "id": 1,
            "command_id": "cmd-1",
            "symbol": "AAPL",
            "status": "Submitted",
            "updated_at": now.isoformat(),
        },
        2: {
            "id": 2,
            "command_id": "cmd-2",
            "symbol": "MSFT",
            "status": "Filled",
            "updated_at": (now - timedelta(minutes=5)).isoformat(),
        },
    }

    runtime, redis, repo = await _create_runtime(repository=repository)
    try:
        result = await runtime.list_orders()
        assert result["total"] == 2
        symbols = {order["symbol"] for order in result["orders"]}
        assert {"AAPL", "MSFT"} <= symbols
        async with runtime._lock:
            identifier = runtime._command_to_order.get("cmd-1")
            assert identifier is not None
            assert runtime._active_orders.get(identifier, {}).get("command_id") == "cmd-1"
            assert any(entry.get("command_id") == "cmd-2" for entry in runtime._history)
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_list_orders_uses_repository_pagination() -> None:
    repository = RecordingRepository()
    now = datetime.now(timezone.utc)
    repository._rows = {
        1: {"id": 1, "command_id": "cmd-1", "symbol": "AAPL", "status": "Submitted", "updated_at": now.isoformat()},
        2: {"id": 2, "command_id": "cmd-2", "symbol": "MSFT", "status": "Filled", "updated_at": (now - timedelta(minutes=1)).isoformat()},
        3: {"id": 3, "command_id": "cmd-3", "symbol": "TSLA", "status": "Filled", "updated_at": (now - timedelta(minutes=2)).isoformat()},
    }

    runtime, redis, repo = await _create_runtime(repository=repository)
    try:
        result = await runtime.list_orders(limit=1, offset=1)
        assert result["total"] == 3
        assert result["orders"]
        assert repo.paginate_calls
        call = repo.paginate_calls[-1]
        assert call["offset"] == 1
        assert call["limit"] == 1
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_skips_existing_stream_entries_on_start() -> None:
    repository = RecordingRepository()
    redis = InMemoryRedis()
    await redis.xadd(
        "stream:ib:results",
        {"type": "order_event", "detail": "legacy"},
    )

    async def redis_factory() -> InMemoryRedis:
        return redis

    async def repository_factory() -> RecordingRepository:
        return repository

    runtime = OrdersRuntime(
        redis_factory=redis_factory,
        order_repository_factory=repository_factory,
        stream_poll_interval=0.0,
        health_interval=0.0,
    )
    await runtime.start()
    try:
        # The runtime should seed the stream cursor to the last existing entry.
        assert runtime._last_message_id == "1-0"
        processed_initial = await runtime.poll_stream_once()
        assert processed_initial == 0

        await redis.xadd(
            "stream:ib:results",
            {"type": "order_event", "detail": "new"},
        )
        processed_after = await runtime.poll_stream_once()
        assert processed_after == 1
        assert runtime._last_message_id == "2-0"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_ignores_non_order_command_results() -> None:
    runtime, redis, repo = await _create_runtime()
    try:
        await redis.xadd(
            "stream:ib:results",
            {
                "command_id": "cmd-non-order",
                "status": "success",
                "result": json.dumps({"message": "pong"}),
            },
        )
        processed = await runtime.poll_stream_once()
        assert processed == 1
        async with runtime._lock:
            assert not runtime._active_orders
            assert "cmd-non-order" not in runtime._command_to_order
        assert repo.insert_calls == 0
        assert repo.update_calls == 0
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_get_order_fetches_from_repository_when_missing() -> None:
    repository = RecordingRepository()
    runtime, redis, repo = await _create_runtime(repository=repository)
    try:
        now = datetime.now(timezone.utc)
        repo._rows[1] = {
            "id": 1,
            "command_id": "cmd-99",
            "symbol": "AMZN",
            "status": "Submitted",
            "updated_at": now.isoformat(),
        }
        payload = await runtime.get_order("cmd-99")
        assert payload["order"]["command_id"] == "cmd-99"
        assert repo.fetch_order_calls[-1] == "cmd-99"
        async with runtime._lock:
            identifier = runtime._command_to_order.get("cmd-99")
            assert identifier is not None
            assert runtime._active_orders.get(identifier, {}).get("command_id") == "cmd-99"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_update_fill_metrics_persists_and_updates_runtime() -> None:
    repository = RecordingRepository()
    runtime, redis, repo = await _create_runtime(repository=repository)
    try:
        submission = OrderSubmission(symbol="AAPL", quantity=10, action="BUY")
        command_id = await runtime.submit_order(submission)
        repo._rows[1]["command_id"] = command_id
        await runtime.update_fill_metrics(
            command_id,
            {
                "filled_quantity": 10,
                "remaining_quantity": 0,
                "avg_fill_price": 150.0,
            },
        )
        assert repo.update_calls >= 1
        order = await runtime.get_order(command_id)
        assert order["order"]["filled_quantity"] == 10
        assert order["order"]["remaining_quantity"] == 0
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_submit_order_persists_to_repository_and_enqueues_command() -> None:
    repository = RecordingRepository()
    runtime, redis, repo = await _create_runtime(repository=repository)
    try:
        submission = OrderSubmission(symbol="MSFT", quantity=5, action="SELL")
        command_id = await runtime.submit_order(submission)
        assert command_id
        assert repo.insert_calls == 1
        queued = await redis.blpop(runtime.command_queue, timeout=0)
        assert queued is not None
        _, raw_payload = queued
        payload = json.loads(raw_payload)
        assert payload["method"] == "place_order"
        assert payload["kwargs"]["symbol"] == "MSFT"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_orders_http_endpoint_returns_runtime_orders() -> None:
    repository = RecordingRepository()
    now = datetime.now(timezone.utc)
    repository._rows = {
        1: {
            "id": 1,
            "command_id": "cmd-1",
            "symbol": "AAPL",
            "status": "Submitted",
            "updated_at": now.isoformat(),
        }
    }
    runtime, redis, repo = await _create_runtime(repository=repository)
    app = create_app(runtime=runtime)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/orders")
    await runtime.stop()
    assert response.status_code == 200
    payload = response.json()
    assert payload["orders"]
    assert payload["orders"][0]["symbol"] == "AAPL"
    assert payload["total"] == 1
