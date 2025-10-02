from __future__ import annotations

import asyncio
import json

import pytest

from src.ibbot.redis import (
    DEFAULT_COMMAND_QUEUE,
    IBCommandExecutionError,
    IBCommandTimeout,
    request_ib_command,
)


class _SimpleRedis:
    """Minimal in-memory Redis replacement for tests."""

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue[str]] = {}
        self._hashes: dict[str, dict[str, str]] = {}
        self._streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self._stream_conditions: dict[str, asyncio.Condition] = {}

    async def flushall(self) -> None:
        self._queues.clear()
        self._hashes.clear()
        self._streams.clear()
        self._stream_conditions.clear()

    async def close(self) -> None:  # pragma: no cover - compatibility stub
        return None

    def _queue(self, key: str) -> asyncio.Queue[str]:
        if key not in self._queues:
            self._queues[key] = asyncio.Queue()
        return self._queues[key]

    def _stream_cond(self, key: str) -> asyncio.Condition:
        if key not in self._stream_conditions:
            self._stream_conditions[key] = asyncio.Condition()
        return self._stream_conditions[key]

    async def rpush(self, key: str, value: str) -> int:
        queue = self._queue(key)
        await queue.put(value)
        return queue.qsize()

    async def blpop(self, key: str, timeout: float | None = None) -> tuple[str, str] | None:
        queue = self._queue(key)
        try:
            if timeout is None or timeout <= 0:
                item = await queue.get()
            else:
                item = await asyncio.wait_for(queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        return key, item

    async def hset(self, key: str, *, mapping: dict[str, str]) -> int:
        existing = self._hashes.setdefault(key, {})
        for field, value in mapping.items():
            existing[str(field)] = str(value)
        return len(mapping)

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self._hashes.get(key, {}))

    async def xadd(self, stream_key: str, message: dict[str, str]) -> str:
        entries = self._streams.setdefault(stream_key, [])
        entry_id = f"{len(entries) + 1}-0"
        cond = self._stream_cond(stream_key)
        async with cond:
            entries.append((entry_id, {str(k): str(v) for k, v in message.items()}))
            cond.notify_all()
        return entry_id

    async def xread(self, streams: dict[str, str], block: int | None = None, count: int | None = None):
        if not streams:
            return []
        stream_key, last_id = next(iter(streams.items()))
        cond = self._stream_cond(stream_key)
        block_seconds = None if block is None else max(block, 0) / 1000.0
        start_len = len(self._streams.get(stream_key, []))

        def _collect() -> list[tuple[str, dict[str, str]]]:
            entries = self._streams.get(stream_key, [])
            if last_id == "$":
                new_entries = entries[start_len:]
            else:
                new_entries = [entry for entry in entries if entry[0] > last_id]
            if count is not None:
                new_entries = new_entries[:count]
            return new_entries

        new_entries = _collect()
        if not new_entries and block_seconds is not None:
            try:
                async with cond:
                    await asyncio.wait_for(cond.wait(), timeout=block_seconds)
            except asyncio.TimeoutError:
                return []
            new_entries = _collect()
        if not new_entries:
            return []
        return [(stream_key, new_entries)]


@pytest.fixture
def event_loop() -> asyncio.AbstractEventLoop:
    loop = asyncio.new_event_loop()
    yield loop
    loop.run_until_complete(loop.shutdown_asyncgens())
    loop.close()


@pytest.fixture
def redis_client(event_loop: asyncio.AbstractEventLoop):
    client = _SimpleRedis()
    event_loop.run_until_complete(client.flushall())
    try:
        yield client
    finally:
        event_loop.run_until_complete(client.flushall())
        event_loop.run_until_complete(client.close())


def test_request_ib_command_success(event_loop, redis_client) -> None:
    async def run_test() -> None:
        async def fake_service() -> None:
            item = await redis_client.blpop(DEFAULT_COMMAND_QUEUE, timeout=1)
            assert item is not None
            payload = json.loads(item[1])
            command_id = payload["command_id"]
            message = {
                "command_id": command_id,
                "status": "success",
                "result": json.dumps({"message": "pong"}),
            }
            await redis_client.xadd("stream:ib:results", message)
            await redis_client.hset(f"hash:ib:result:{command_id}", mapping=message)

        producer = asyncio.create_task(fake_service())
        record = await request_ib_command("ping", redis=redis_client, timeout=1)
        await producer

        assert record["status"] == "success"
        assert record["result"] == {"message": "pong"}
        assert record["command_id"]

    event_loop.run_until_complete(run_test())


def test_request_ib_command_error(event_loop, redis_client) -> None:
    async def run_test() -> None:
        async def fake_service() -> None:
            item = await redis_client.blpop(DEFAULT_COMMAND_QUEUE, timeout=1)
            assert item is not None
            payload = json.loads(item[1])
            command_id = payload["command_id"]
            message = {
                "command_id": command_id,
                "status": "error",
                "error": "boom",
            }
            await redis_client.xadd("stream:ib:results", message)
            await redis_client.hset(f"hash:ib:result:{command_id}", mapping=message)

        consumer = asyncio.create_task(fake_service())
        with pytest.raises(IBCommandExecutionError) as exc:
            await request_ib_command("ping", redis=redis_client, timeout=1)

        await consumer

        assert exc.value.command_id
        assert "boom" in str(exc.value)
        assert exc.value.payload["status"] == "error"
        assert exc.value.payload["error"] == "boom"

    event_loop.run_until_complete(run_test())


def test_request_ib_command_timeout(event_loop, redis_client) -> None:
    async def run_test() -> None:
        with pytest.raises(IBCommandTimeout) as exc:
            await request_ib_command("ping", redis=redis_client, timeout=0.2)

        assert exc.value.command_id
        assert exc.value.payload["method"] == "ping"

    event_loop.run_until_complete(run_test())
