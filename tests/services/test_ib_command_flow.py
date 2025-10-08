import asyncio
import json
import time
from collections import deque
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from services.ib_command import (
    CommandConsumer,
    ExecutionResult,
    IBCommandExecutionError,
    IBCommandExecutor,
    ResultPublisher,
    RetryableIBCommandError,
)
from services.ib_command.components import RedisError
from src.ibkr_client import IBKRClient


class _InMemoryRedis:
    def __init__(self) -> None:
        self._queues: dict[str, list[str]] = {}
        self._streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self._hashes: dict[str, dict[str, str]] = {}
        self._message_counter = 0

    async def flushall(self) -> None:
        self._queues.clear()
        self._streams.clear()
        self._hashes.clear()
        self._message_counter = 0

    async def close(self) -> None:
        return None

    async def rpush(self, key: str, value: str) -> int:
        queue = self._queues.setdefault(key, [])
        queue.append(value)
        return len(queue)

    async def blpop(self, key: str, timeout: float | int | None = 0) -> tuple[str, str] | None:
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        while True:
            queue = self._queues.setdefault(key, [])
            if queue:
                value = queue.pop(0)
                return key, value
            if deadline is not None and time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.01)

    async def xadd(self, key: str, fields: dict[str, str]) -> str:
        self._message_counter += 1
        message_id = f"{self._message_counter}-0"
        stream = self._streams.setdefault(key, [])
        stream.append((message_id, dict(fields)))
        return message_id

    async def hset(self, key: str, *, mapping: dict[str, str]) -> int:
        current = self._hashes.setdefault(key, {})
        current.update(mapping)
        return len(mapping)

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self._hashes.get(key, {}))

    async def xrange(self, key: str) -> list[tuple[str, dict[str, str]]]:
        return list(self._streams.get(key, []))

    async def llen(self, key: str) -> int:
        return len(self._queues.get(key, []))


@pytest.fixture
def redis_client():
    try:
        import fakeredis.aioredis as fakeredis  # type: ignore

        client = fakeredis.FakeRedis(decode_responses=True)
    except ModuleNotFoundError:
        client = _InMemoryRedis()
    asyncio.run(client.flushall())
    try:
        yield client
    finally:
        asyncio.run(client.flushall())
        asyncio.run(client.close())


class MockIB:
    def __init__(self) -> None:
        self.calls: deque[tuple[str, Any]] = deque()
        self._flaky_attempts = 0
        self._processed = asyncio.Event()
        self._expected = 0

    def expect(self, count: int) -> None:
        self._expected = count
        if count == 0:
            self._processed.set()
        else:
            self._processed.clear()

    async def wait(self, timeout: float = 2.0) -> None:
        await asyncio.wait_for(self._processed.wait(), timeout=timeout)

    def _record(self, method: str, payload: Any) -> None:
        self.calls.append((method, payload))
        if len(self.calls) >= self._expected:
            self._processed.set()

    async def stable(self, payload: Any) -> dict[str, Any]:
        self._record("stable", payload)
        return {"echo": payload}

    async def flaky(self, payload: Any) -> dict[str, Any]:
        self._record("flaky", payload)
        self._flaky_attempts += 1
        if self._flaky_attempts == 1:
            raise RetryableIBCommandError("temporary glitch")
        return {"echo": payload, "attempt": self._flaky_attempts}

    async def fatal(self, payload: Any) -> None:
        self._record("fatal", payload)
        raise IBCommandExecutionError("boom", error_code=9001)


def test_command_flow_sequential_retry_and_failure(redis_client):
    asyncio.run(_run_flow_test(redis_client))


async def _run_flow_test(redis_client: "fakeredis.aioredis.FakeRedis") -> None:  # type: ignore[name-defined]
    ib = MockIB()
    executor = IBCommandExecutor(ib, logger_name="tests.ib_command.executor")
    publisher = ResultPublisher(
        redis_client,
        stream_key="stream:test:results",
        hash_prefix="hash:test:result",
        logger_name="tests.ib_command.results",
    )
    consumer = CommandConsumer(
        redis_client,
        executor,
        publisher,
        queue_key="queue:test:commands",
        poll_timeout=0.1,
        default_max_retries=2,
        retry_delay=0.05,
        health_key="hash:service:health:test",
        logger_name="tests.ib_command.consumer",
    )

    commands = [
        {"command_id": "cmd-1", "method": "stable", "args": ["A"]},
        {"command_id": "cmd-2", "method": "flaky", "args": ["B"], "max_retries": 2},
        {"command_id": "cmd-3", "method": "fatal", "args": ["C"], "max_retries": 1},
    ]

    for command in commands:
        await redis_client.rpush("queue:test:commands", json.dumps(command))

    ib.expect(len(commands))
    task = asyncio.create_task(consumer.consume_forever())
    await ib.wait()
    await asyncio.sleep(0.2)
    consumer.stop()
    await asyncio.wait_for(task, timeout=2.0)

    assert list(ib.calls) == [
        ("stable", "A"),
        ("flaky", "B"),
        ("flaky", "B"),
        ("fatal", "C"),
    ]

    stream_entries = await redis_client.xrange("stream:test:results")
    statuses = [entry[1].get("status") for entry in stream_entries]
    assert statuses == ["success", "success", "failed"]

    success_payload = await redis_client.hgetall("hash:test:result:cmd-2")
    assert success_payload["attempts"] == "2"
    assert json.loads(success_payload["result"])["attempt"] == 2

    failure_payload = await redis_client.hgetall("hash:test:result:cmd-3")
    assert failure_payload["status"] == "failed"
    assert "boom" in failure_payload["error"]
    assert failure_payload.get("error_code") == "9001"

    health = await redis_client.hgetall("hash:service:health:test")
    assert health["queue_depth"] == "0"
    assert "boom" in health["last_error"]
    assert health.get("last_error_code") == "9001"


def test_consumer_recovers_from_publish_failures(redis_client) -> None:
    asyncio.run(_run_publish_failure_test(redis_client))


async def _run_publish_failure_test(redis_client: "fakeredis.aioredis.FakeRedis") -> None:  # type: ignore[name-defined]
    class FlakyPublisher(ResultPublisher):
        def __init__(self, redis: Any) -> None:
            super().__init__(
                redis,
                stream_key="stream:test:results",
                hash_prefix="hash:test:result",
                logger_name="tests.ib_command.flaky_publisher",
            )
            self._failures_remaining = 1

        async def publish(self, payload: ExecutionResult) -> None:  # type: ignore[override]
            if self._failures_remaining > 0:
                self._failures_remaining -= 1
                raise RedisError("simulated publish failure")
            await super().publish(payload)

    ib = MockIB()
    executor = IBCommandExecutor(ib, logger_name="tests.ib_command.executor")
    publisher = FlakyPublisher(redis_client)
    consumer = CommandConsumer(
        redis_client,
        executor,
        publisher,
        queue_key="queue:test:commands",
        poll_timeout=0.05,
        default_max_retries=0,
        retry_delay=0.01,
        health_key="hash:service:health:test",
        logger_name="tests.ib_command.consumer",
        publish_retry_attempts=3,
    )

    command = {"command_id": "cmd-flaky", "method": "stable", "args": ["payload"]}
    await redis_client.rpush("queue:test:commands", json.dumps(command))

    ib.expect(1)
    task = asyncio.create_task(consumer.consume_forever())
    await ib.wait()

    result_key = "hash:test:result:cmd-flaky"
    await _wait_for_hash(redis_client, result_key, timeout=2.0)

    consumer.stop()
    await asyncio.wait_for(task, timeout=2.0)

    payload = await redis_client.hgetall(result_key)
    assert payload.get("status") == "success"
    assert payload.get("command_id") == "cmd-flaky"


async def _wait_for_hash(redis_client: Any, key: str, timeout: float = 2.0) -> dict[str, str]:
    deadline = time.monotonic() + timeout
    while True:
        payload = await redis_client.hgetall(key)
        if payload:
            return payload
        if time.monotonic() >= deadline:
            raise AssertionError(f"Timed out waiting for Redis hash {key}")
        await asyncio.sleep(0.05)


async def _run_place_order_flow(redis_client: Any) -> None:
    await redis_client.flushall()
    client = IBKRClient()
    queue_key = "queue:test:commands:place-order"
    stream_key = "stream:test:results:place-order"
    hash_prefix = "hash:test:result:place-order"

    executor = IBCommandExecutor(client, logger_name="tests.ib_command.executor.place_order")
    publisher = ResultPublisher(
        redis_client,
        stream_key=stream_key,
        hash_prefix=hash_prefix,
        logger_name="tests.ib_command.results.place_order",
    )
    consumer = CommandConsumer(
        redis_client,
        executor,
        publisher,
        queue_key=queue_key,
        poll_timeout=0.05,
        default_max_retries=0,
        retry_delay=0.05,
        health_key="hash:service:health:test-place-order",
        logger_name="tests.ib_command.consumer.place_order",
    )

    command_id = "cmd-place-order"
    order_payload = {
        "command_id": command_id,
        "method": "place_order",
        "kwargs": {
            "symbol": "ES",
            "quantity": 1,
            "action": "BUY",
            "order_type": "LIMIT",
            "limit_price": 4450.25,
            "exchange": "CME",
            "sec_type": "FUT",
        },
    }
    await redis_client.rpush(queue_key, json.dumps(order_payload))

    task = asyncio.create_task(consumer.consume_forever())
    result_hash_key = f"{hash_prefix}:{command_id}"
    try:
        result_entry = await _wait_for_hash(redis_client, result_hash_key, timeout=2.0)
    finally:
        consumer.stop()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.CancelledError:
            pass
        await client.disconnect()

    assert result_entry.get("status") == "success"
    assert "result" in result_entry
    order_result = json.loads(result_entry["result"])
    assert isinstance(order_result, dict)
    assert order_result.get("order_id")
    assert order_result.get("status") == "Simulated"
    assert order_result.get("symbol") == "ES"
    assert order_result.get("exchange") == "CME"
    assert order_result.get("sec_type") == "FUT"
    assert order_result.get("order_type") == "LIMIT"
    assert pytest.approx(order_result.get("limit_price") or 0.0, rel=1e-6) == 4450.25
    assert pytest.approx(order_result.get("quantity") or 0.0, rel=1e-6) == 1.0
    assert pytest.approx(order_result.get("remaining") or 0.0, rel=1e-6) == 1.0
    assert order_result.get("filled") in (0, 0.0, None)
    assert order_result.get("avg_fill_price") is None


def test_place_order_command_returns_mapping(redis_client):
    asyncio.run(_run_place_order_flow(redis_client))


async def _run_trade_serialisation_test(redis_client: Any) -> None:
    stream_key = "stream:test:results:trade"
    hash_prefix = "hash:test:result:trade"
    publisher = ResultPublisher(
        redis_client,
        stream_key=stream_key,
        hash_prefix=hash_prefix,
        logger_name="tests.ib_command.results.trade",
    )

    trade = _build_fake_trade()
    payload = ExecutionResult(
        command_id="cmd-trade",
        status="success",
        result=trade,
        attempts=1,
    )
    await publisher.publish(payload)

    entries = await redis_client.xrange(stream_key)
    assert entries
    _, message = entries[0]
    result_payload = json.loads(message.get("result", "{}"))
    assert result_payload["order_id"] == "123"
    assert result_payload["status"] == "Filled"
    assert result_payload["symbol"] == "ESH4"
    assert result_payload["commission_currency"] == "USD"
    assert pytest.approx(result_payload["commission"], rel=1e-6) == 1.25
    assert pytest.approx(result_payload["realized_pnl"], rel=1e-6) == 25.0
    assert result_payload["fills"]
    fill_payload = result_payload["fills"][0]
    assert fill_payload["exec_id"] == "abc-1"
    assert pytest.approx(fill_payload["price"], rel=1e-6) == 101.25
    assert pytest.approx(fill_payload["quantity"], rel=1e-6) == 2.0

    hash_payload = await redis_client.hgetall(f"{hash_prefix}:cmd-trade")
    assert hash_payload["status"] == "success"
    assert json.loads(hash_payload["result"]) == result_payload


def _build_fake_trade() -> SimpleNamespace:
    timestamp = datetime(2024, 1, 1, 14, 30, tzinfo=timezone.utc)
    order = SimpleNamespace(
        orderId=123,
        permId=456,
        action="BUY",
        totalQuantity=2,
        orderType="LIMIT",
        lmtPrice=100.5,
    )
    order_status = SimpleNamespace(
        status="Filled",
        avgFillPrice=101.25,
        filled=2,
        remaining=0,
        commission=1.25,
        commissionCurrency="USD",
        realizedPNL=25.0,
        submittedTime=timestamp,
        completedTime=timestamp,
    )
    contract = SimpleNamespace(
        symbol="ES",
        localSymbol="ESH4",
        exchange="CME",
        secType="FUT",
    )
    execution = SimpleNamespace(
        execId="abc-1",
        time=timestamp,
        side="BOT",
        price=101.25,
        avgPrice=101.25,
        shares=2,
        exchange="CME",
        acctNumber="DU123",
        orderId=123,
    )
    commission_report = SimpleNamespace(
        commission=1.25,
        currency="USD",
        realizedPNL=25.0,
    )
    fill = SimpleNamespace(execution=execution, commissionReport=commission_report)
    log_entry = SimpleNamespace(time=timestamp)
    return SimpleNamespace(
        order=order,
        orderStatus=order_status,
        contract=contract,
        fills=[fill],
        log=[log_entry],
    )


def test_result_publisher_serialises_trades(redis_client):
    asyncio.run(_run_trade_serialisation_test(redis_client))
