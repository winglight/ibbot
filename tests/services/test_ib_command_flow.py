import asyncio
import json
import time
from collections import deque
from typing import Any

import pytest

from services.ib_command import (
    CommandConsumer,
    IBCommandExecutionError,
    IBCommandExecutor,
    ResultPublisher,
    RetryableIBCommandError,
)


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
        raise IBCommandExecutionError("boom")


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

    health = await redis_client.hgetall("hash:service:health:test")
    assert health["queue_depth"] == "0"
    assert "boom" in health["last_error"]
