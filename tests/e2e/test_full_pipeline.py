import asyncio
import json
from typing import Any

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from services.ib_command import CommandConsumer, IBCommandExecutor, ResultPublisher
from services.ib_command.components import ExecutionResult

from src.routes import orders as orders_module


class InMemoryRedis:
    """Minimal async Redis clone for end-to-end testing."""

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue[str]] = {}
        self._streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self._hashes: dict[str, dict[str, str]] = {}
        self._message_counter = 0

    async def rpush(self, key: str, value: str) -> int:
        queue = self._queues.setdefault(key, asyncio.Queue())
        await queue.put(value)
        return queue.qsize()

    async def blpop(
        self,
        key: str,
        timeout: float | int | None = 0,
    ) -> tuple[str, str] | None:
        queue = self._queues.setdefault(key, asyncio.Queue())
        if timeout is None:
            value = await queue.get()
            return key, value

        try:
            value = await asyncio.wait_for(queue.get(), timeout=float(timeout))
        except asyncio.TimeoutError:
            return None
        return key, value

    async def xadd(self, key: str, fields: dict[str, str]) -> str:
        self._message_counter += 1
        message_id = f"{self._message_counter}-0"
        stream = self._streams.setdefault(key, [])
        stream.append((message_id, dict(fields)))
        return message_id

    async def xrange(self, key: str) -> list[tuple[str, dict[str, str]]]:
        return list(self._streams.get(key, []))

    async def hset(self, key: str, *, mapping: dict[str, Any]) -> int:
        normalised = {str(k): str(v) for k, v in mapping.items()}
        current = self._hashes.setdefault(key, {})
        current.update(normalised)
        return len(normalised)

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self._hashes.get(key, {}))


class DummyWebSocketManager:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def broadcast(self, message: dict[str, Any]) -> None:
        self.messages.append(message)


class FakeInflux:
    def __init__(self) -> None:
        self.points: list[tuple[str, dict[str, Any]]] = []

    async def write_point(self, measurement: str, fields: dict[str, Any]) -> None:
        self.points.append((measurement, dict(fields)))


class ObservedResultPublisher(ResultPublisher):
    def __init__(
        self,
        redis: InMemoryRedis,
        *,
        websocket_manager: DummyWebSocketManager,
        influx: FakeInflux,
        stream_key: str,
        hash_prefix: str,
    ) -> None:
        super().__init__(
            redis,
            stream_key=stream_key,
            hash_prefix=hash_prefix,
            logger_name="tests.ib_command.results",
        )
        self._websocket_manager = websocket_manager
        self._influx = influx

    async def publish(self, payload: ExecutionResult) -> None:  # type: ignore[override]
        await super().publish(payload)
        await self._influx.write_point(
            "order_results",
            {
                "command_id": payload.command_id,
                "status": payload.status,
                "attempts": payload.attempts,
            },
        )
        await self._websocket_manager.broadcast(
            {
                "type": "order_command_result",
                "command_id": payload.command_id,
                "status": payload.status,
                "attempts": payload.attempts,
            }
        )


class FakeIB:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def place_market_order(
        self,
        *,
        symbol: str,
        quantity: int,
        action: str,
        exchange: str,
        sec_type: str,
    ) -> dict[str, Any]:
        payload = {
            "symbol": symbol,
            "quantity": quantity,
            "action": action,
            "exchange": exchange,
            "sec_type": sec_type,
        }
        self.calls.append(payload)
        return {
            "status": "filled",
            "filled": quantity,
            "symbol": symbol,
        }


async def _wait_for_result(
    redis: InMemoryRedis,
    hash_prefix: str,
    command_id: str,
    *,
    timeout: float = 2.0,
) -> dict[str, str]:
    deadline = asyncio.get_event_loop().time() + timeout
    key = f"{hash_prefix}:{command_id}"
    while True:
        payload = await redis.hgetall(key)
        if payload.get("status"):
            return payload
        if asyncio.get_event_loop().time() >= deadline:
            raise TimeoutError(f"Result for {command_id} not available")
        await asyncio.sleep(0.05)


async def _wait_for_messages(
    manager: DummyWebSocketManager,
    expected_count: int,
    *,
    timeout: float = 2.0,
) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        if len(manager.messages) >= expected_count:
            return
        if asyncio.get_event_loop().time() >= deadline:
            raise TimeoutError("Websocket messages not emitted in time")
        await asyncio.sleep(0.05)


def test_order_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_run_order_flow(monkeypatch))


async def _run_order_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = InMemoryRedis()
    websocket_manager = DummyWebSocketManager()
    influx = FakeInflux()
    fake_ib = FakeIB()

    async def allow_user(request: Request) -> str:
        request.state.is_read_only = False
        request.state.user_role = "admin"
        request.state.username = "tester"
        return "tester"

    async def noop(*_: Any, **__: Any) -> None:
        return None

    queue_key = "queue:test:commands"
    stream_key = "stream:test:results"
    hash_prefix = "hash:test:result"
    recorded_payloads: list[dict[str, Any]] = []

    async def fake_enqueue(payload: dict[str, Any]) -> None:
        recorded_payloads.append(dict(payload))
        await redis.rpush(queue_key, json.dumps(payload))

    class _FakeUUID:
        hex = "cmd-test-001"

    app = FastAPI()
    app.include_router(orders_module.router)
    app.dependency_overrides[orders_module.get_current_user] = allow_user

    monkeypatch.setattr(orders_module, "enqueue_ib_command", fake_enqueue)
    monkeypatch.setattr(orders_module, "websocket_manager", websocket_manager)
    monkeypatch.setattr(orders_module.uuid, "uuid4", lambda: _FakeUUID())
    monkeypatch.setattr(orders_module, "audit_log_success", noop)
    monkeypatch.setattr(orders_module, "audit_log_failure", noop)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/place_order",
            json={
                "symbol": "ES",
                "quantity": 1,
                "action": "BUY",
                "order_type": "market",
                "exchange": "CME",
                "sec_type": "FUT",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["command_id"] == "cmd-test-001"
    assert body["status"] == "queued"

    assert recorded_payloads and recorded_payloads[0]["command_id"] == "cmd-test-001"

    executor = IBCommandExecutor(fake_ib, logger_name="tests.ib_command.executor")
    publisher = ObservedResultPublisher(
        redis,
        websocket_manager=websocket_manager,
        influx=influx,
        stream_key=stream_key,
        hash_prefix=hash_prefix,
    )
    consumer = CommandConsumer(
        redis,
        executor,
        publisher,
        queue_key=queue_key,
        poll_timeout=0.05,
        default_max_retries=0,
        retry_delay=0.01,
        health_key="hash:service:health:test",
        logger_name="tests.ib_command.consumer",
    )

    consumer_task = asyncio.create_task(consumer.consume_forever())

    try:
        result_payload = await _wait_for_result(redis, hash_prefix, "cmd-test-001")
        await _wait_for_messages(websocket_manager, 2)
    finally:
        consumer.stop()
        await asyncio.wait_for(consumer_task, timeout=2.0)

    assert fake_ib.calls == [
        {
            "symbol": "ES",
            "quantity": 1,
            "action": "BUY",
            "exchange": "CME",
            "sec_type": "FUT",
        }
    ]

    assert result_payload["command_id"] == "cmd-test-001"
    assert result_payload["status"] == "success"

    assert influx.points == [
        (
            "order_results",
            {
                "command_id": "cmd-test-001",
                "status": "success",
                "attempts": 1,
            },
        )
    ]

    assert websocket_manager.messages[0]["type"] == "order_command_enqueued"
    assert websocket_manager.messages[0]["command_id"] == "cmd-test-001"

    assert websocket_manager.messages[-1] == {
        "type": "order_command_result",
        "command_id": "cmd-test-001",
        "status": "success",
        "attempts": 1,
    }

