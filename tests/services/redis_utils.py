"""Utility helpers for service tests that require a Redis-like client."""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Any


def _parse_stream_id(value: str | bytes | bytearray) -> tuple[int, int]:
    if isinstance(value, (bytes, bytearray)):
        text = value.decode("utf-8", errors="ignore")
    else:
        text = str(value)
    if text in {"-", "+"}:
        return (0 if text == "-" else 2**63, 0)
    major, _, minor = text.partition("-")
    try:
        return int(major), int(minor or 0)
    except ValueError:
        return 0, 0


class InMemoryRedis:
    """Minimal async Redis clone backed by in-memory Python structures."""

    def __init__(self) -> None:
        self._queues: dict[str, deque[str]] = {}
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
        queue = self._queues.setdefault(key, deque())
        queue.append(value)
        return len(queue)

    async def blpop(self, key: str, timeout: float | int | None = 0) -> tuple[str, str] | None:
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        while True:
            queue = self._queues.setdefault(key, deque())
            if queue:
                value = queue.popleft()
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

    async def xrange(
        self,
        key: str,
        min: str = "-",
        max: str = "+",
        count: int | None = None,
    ) -> list[tuple[str, dict[str, str]]]:
        stream = list(self._streams.get(key, []))
        min_exclusive = False
        if min.startswith("("):
            min_exclusive = True
            min = min[1:]
        max_exclusive = False
        if max.startswith("("):
            max_exclusive = True
            max = max[1:]
        min_id = _parse_stream_id(min)
        max_id = _parse_stream_id(max)
        results: list[tuple[str, dict[str, str]]] = []
        for message_id, payload in stream:
            message_id_tuple = _parse_stream_id(message_id)
            if message_id_tuple < min_id or (
                min_exclusive and message_id_tuple == min_id
            ):
                continue
            if message_id_tuple > max_id or (
                max_exclusive and message_id_tuple == max_id
            ):
                continue
            results.append((message_id, dict(payload)))
            if count is not None and len(results) >= count:
                break
        return results

    async def xrevrange(
        self,
        key: str,
        max: str = "+",
        min: str = "-",
        count: int | None = None,
    ) -> list[tuple[str, dict[str, str]]]:
        stream = list(reversed(self._streams.get(key, [])))
        min_exclusive = False
        if min.startswith("("):
            min_exclusive = True
            min = min[1:]
        max_exclusive = False
        if max.startswith("("):
            max_exclusive = True
            max = max[1:]
        min_id = _parse_stream_id(min)
        max_id = _parse_stream_id(max)
        results: list[tuple[str, dict[str, str]]] = []
        for message_id, payload in stream:
            message_id_tuple = _parse_stream_id(message_id)
            if message_id_tuple > max_id or (
                max_exclusive and message_id_tuple == max_id
            ):
                continue
            if message_id_tuple < min_id or (
                min_exclusive and message_id_tuple == min_id
            ):
                continue
            results.append((message_id, dict(payload)))
            if count is not None and len(results) >= count:
                break
        return results

    async def hset(self, key: str, *, mapping: dict[str, Any]) -> int:
        normalised = {str(k): str(v) for k, v in mapping.items()}
        current = self._hashes.setdefault(key, {})
        current.update(normalised)
        return len(normalised)

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self._hashes.get(key, {}))

    async def llen(self, key: str) -> int:
        queue = self._queues.get(key)
        return len(queue) if queue else 0

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        queue = self._queues.get(key, deque())
        items = list(queue)
        end = len(items) if stop == -1 else stop + 1
        return items[start:end]

    async def set(self, key: str, value: Any) -> bool:
        self._hashes.setdefault("__kv__", {})[key] = json.dumps(value)
        return True

    async def get(self, key: str) -> Any:
        value = self._hashes.get("__kv__", {}).get(key)
        if value is None:
            return None
        return json.loads(value)

    async def delete(self, *keys: str) -> int:
        removed = 0
        kv = self._hashes.get("__kv__", {})
        for key in keys:
            if key in self._queues:
                del self._queues[key]
                removed += 1
            if key in self._streams:
                del self._streams[key]
                removed += 1
            if key in self._hashes and key != "__kv__":
                del self._hashes[key]
                removed += 1
            if key in kv:
                del kv[key]
                removed += 1
        return removed
