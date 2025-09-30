import asyncio
import inspect
import json
from typing import Any, Mapping

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from services.ib_command import CommandConsumer, IBCommandExecutor, ResultPublisher
from services.ib_command.components import ExecutionResult
from services.market_data.main import MarketDataRuntime
from services.risk import RiskRuntime
from services.strategy import StrategyAdapter, StrategyTaskConsumer

from src.routes import orders as orders_module


class InMemoryRedis:
    """Minimal async Redis clone for end-to-end testing."""

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue[str]] = {}
        self._streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self._hashes: dict[str, dict[str, str]] = {}
        self._message_counter = 0
        self._groups: dict[tuple[str, str], int] = {}

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

    async def xadd(
        self,
        key: str,
        fields: dict[str, str],
        *,
        maxlen: int | None = None,
        approximate: bool = False,
    ) -> str:
        self._message_counter += 1
        message_id = f"{self._message_counter}-0"
        stream = self._streams.setdefault(key, [])
        stream.append((message_id, dict(fields)))
        if maxlen is not None and maxlen > 0:
            limit = maxlen if not approximate else maxlen + 1
            if len(stream) > limit:
                del stream[:-maxlen]
        return message_id

    async def xrange(self, key: str) -> list[tuple[str, dict[str, str]]]:
        return list(self._streams.get(key, []))

    async def xgroup_create(
        self,
        stream: str,
        group: str,
        *,
        id: str = "0",
        mkstream: bool = False,
    ) -> None:
        if mkstream:
            self._streams.setdefault(stream, [])
        self._groups[(stream, group)] = len(self._streams.get(stream, []))

    async def xreadgroup(
        self,
        *,
        groupname: str,
        consumername: str,
        streams: dict[str, str],
        count: int | None = None,
        block: int | None = None,
    ) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        results: list[tuple[str, list[tuple[str, dict[str, str]]]]] = []
        for stream, _ in streams.items():
            entries = self._streams.get(stream, [])
            index = self._groups.get((stream, groupname), 0)
            if index >= len(entries):
                continue
            batch = entries[index:]
            if count is not None:
                batch = batch[:count]
            if not batch:
                continue
            self._groups[(stream, groupname)] = index + len(batch)
            results.append((stream, [(message_id, dict(payload)) for message_id, payload in batch]))
        if not results and block:
            await asyncio.sleep(block / 1000.0)
        return results

    async def xack(self, stream: str, group: str, *message_ids: str) -> int:
        return len(message_ids)

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

    async def submit_risk_close(
        self,
        *,
        symbol: str,
        reason: str,
        price: float,
    ) -> dict[str, Any]:
        payload = {
            "method": "submit_risk_close",
            "symbol": symbol,
            "reason": reason,
            "price": price,
        }
        self.calls.append(payload)
        return {
            "status": "submitted",
            "symbol": symbol,
            "reason": reason,
            "price": price,
        }


class DummyPubSubListener:
    def __init__(self) -> None:
        self._callbacks: dict[str, list[Any]] = {}

    def register_callback(self, channel: str, callback: Any) -> None:
        self._callbacks.setdefault(channel, []).append(callback)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def emit(self, channel: str, payload: str) -> None:
        for callback in self._callbacks.get(channel, []):
            result = callback(channel, payload)
            if inspect.isawaitable(result):
                await result


class RecordingWriter:
    def __init__(self) -> None:
        self.points: list[dict[str, Any]] = []

    async def __call__(self, points: Any) -> int:
        batch = list(points)
        self.points.extend(batch)
        return len(batch)


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


async def _wait_for_stream_entries(
    redis: InMemoryRedis,
    stream_key: str,
    expected_count: int,
    *,
    timeout: float = 2.0,
) -> list[tuple[str, dict[str, str]]]:
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        entries = await redis.xrange(stream_key)
        if len(entries) >= expected_count:
            return entries
        if asyncio.get_event_loop().time() >= deadline:
            raise TimeoutError(f"Stream {stream_key} did not reach {expected_count} entries")
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


def test_market_strategy_risk_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_run_market_strategy_risk_flow(monkeypatch))


async def _run_market_strategy_risk_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = InMemoryRedis()
    websocket_manager = DummyWebSocketManager()
    influx = FakeInflux()
    fake_ib = FakeIB()
    pubsub = DummyPubSubListener()
    writer = RecordingWriter()

    queue_key = "queue:ib:commands"
    result_stream = "stream:ib:results"
    result_hash_prefix = "hash:ib:result"
    market_stream = "stream:market-service"
    strategy_queue = "list:queue:strategy"
    strategy_event_stream = "stream:strategy:events"

    async def redis_factory() -> InMemoryRedis:
        return redis

    market_runtime = MarketDataRuntime(
        redis_factory=redis_factory,
        write_fn=writer,
        pubsub_listener=pubsub,
        subscriptions=("ES",),
        stream_key=market_stream,
        service_name="market-data-service-test",
        logger_name="tests.market_data.runtime",
        metrics_interval=0.1,
    )

    original_append_stream = MarketDataRuntime._append_stream

    async def _patched_append_stream(
        self: MarketDataRuntime,
        symbol: str,
        message_type: str,
        components: Mapping[str, Any],
    ) -> None:
        updated_components = components
        if message_type == "price":
            price_data = components.get("price") if isinstance(components, Mapping) else None
            price_value: float | None = None
            if isinstance(price_data, Mapping):
                raw_value = price_data.get("price") or price_data.get("last")
                if raw_value is not None:
                    try:
                        price_value = float(raw_value)
                    except (TypeError, ValueError):
                        price_value = None
            if price_value is not None:
                mutable = dict(components)
                mutable["last"] = price_value
                updated_components = mutable
        await original_append_stream(self, symbol, message_type, updated_components)

    monkeypatch.setattr(MarketDataRuntime, "_append_stream", _patched_append_stream)

    class ThresholdStrategy:
        def __init__(self, config: dict[str, Any]) -> None:
            self._threshold = float(config.get("threshold", 0.0))
            self._size = int(config.get("size", 1))
            self._action = str(config.get("action", "BUY"))
            self._exchange = str(config.get("exchange", "CME"))
            self._sec_type = str(config.get("sec_type", "FUT"))

        async def run(self, *, context: Any, task: Mapping[str, Any], config: Mapping[str, Any]) -> Mapping[str, Any]:
            symbol = str(task.get("symbol"))
            price = await context.get_price(symbol)
            if price > self._threshold:
                return {}
            context.emit_signal({"type": "entry", "symbol": symbol, "price": price})
            order_payload = {
                "command_id": f"{task['task_id']}-order",
                "method": "place_market_order",
                "kwargs": {
                    "symbol": symbol,
                    "quantity": self._size,
                    "action": self._action,
                    "exchange": self._exchange,
                    "sec_type": self._sec_type,
                },
            }
            return {"orders": [order_payload]}

    strategies = {
        "threshold": {
            "factory": lambda config: ThresholdStrategy(config),
            "params": {
                "threshold": 95.0,
                "size": 1,
                "action": "BUY",
                "exchange": "CME",
                "sec_type": "FUT",
            },
        }
    }

    strategy_adapter = StrategyAdapter(
        redis,
        strategies=strategies,
        command_queue=queue_key,
        event_stream=strategy_event_stream,
        snapshot_prefix="hash:market:snapshot",
    )
    strategy_consumer = StrategyTaskConsumer(
        redis,
        strategy_adapter,
        queue_key=strategy_queue,
        poll_timeout=0.05,
    )

    risk_runtime = RiskRuntime(
        redis_factory=redis_factory,
        write_fn=writer,
        logger_name="tests.risk-service",
        service_name="tests.risk-service",
        poll_interval=0.05,
    )
    risk_config = {
        "rules": json.dumps([{"symbol": "ES", "stop_loss": 95.0}]),
        "market_stream": market_stream,
        "market_group": "group:risk:market",
        "results_stream": result_stream,
        "results_group": "group:risk:results",
        "command_queue": queue_key,
        "batch_size": 5,
    }
    await risk_runtime.update_config(risk_config)

    executor = IBCommandExecutor(fake_ib, logger_name="tests.ib_command.executor")
    publisher = ObservedResultPublisher(
        redis,
        websocket_manager=websocket_manager,
        influx=influx,
        stream_key=result_stream,
        hash_prefix=result_hash_prefix,
    )
    command_consumer = CommandConsumer(
        redis,
        executor,
        publisher,
        queue_key=queue_key,
        poll_timeout=0.05,
        default_max_retries=0,
        retry_delay=0.01,
        health_key="hash:service:health:e2e",
        logger_name="tests.ib_command.consumer",
    )

    await market_runtime.start()
    consumer_task = asyncio.create_task(command_consumer.consume_forever())
    strategy_task = asyncio.create_task(strategy_consumer.run_forever())
    await risk_runtime.start()

    try:
        await redis.hset("hash:market:snapshot:ES", mapping={"price": "94.5"})

        price_channel = "pubsub:ib:price:ES"
        await pubsub.emit(price_channel, json.dumps({"symbol": "ES", "price": 94.5}))

        task_payload = {"task_id": "job-1", "strategy": "threshold", "symbol": "ES"}
        await redis.rpush(strategy_queue, json.dumps(task_payload))

        market_entries = await _wait_for_stream_entries(redis, market_stream, 1)
        assert market_entries[0][1]["symbol"] == "ES"
        assert market_entries[0][1]["type"] == "price"

        strategy_events = await _wait_for_stream_entries(redis, strategy_event_stream, 2)
        assert [event[1]["status"] for event in strategy_events] == ["started", "completed"]
        signals_payload = json.loads(strategy_events[-1][1]["signals"])
        assert signals_payload and signals_payload[0]["type"] == "entry"

        await _wait_for_result(redis, result_hash_prefix, "job-1-order")
        result_entries = await _wait_for_stream_entries(redis, result_stream, 2)
        result_by_id = {entry[1]["command_id"]: entry[1] for entry in result_entries}
        assert "job-1-order" in result_by_id

        risk_entry = None
        for entry in result_entries:
            payload = entry[1]
            body_text = payload.get("result")
            data = json.loads(body_text) if body_text else {}
            if data.get("reason") == "risk_stop_loss":
                risk_entry = payload
                break

        assert risk_entry is not None, "Risk result was not published"
        risk_command_id = risk_entry["command_id"]
        risk_result_hash = await _wait_for_result(redis, result_hash_prefix, risk_command_id)
        assert risk_result_hash["status"] == "success"

        assert any(call.get("reason") == "risk_stop_loss" for call in fake_ib.calls)

        assert len(result_entries) == 2
    finally:
        command_consumer.stop()
        strategy_consumer.stop()
        await asyncio.wait_for(consumer_task, timeout=2.0)
        await asyncio.wait_for(strategy_task, timeout=2.0)
        await risk_runtime.stop()
        await market_runtime.stop()
