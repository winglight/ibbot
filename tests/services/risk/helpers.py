from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping


class CapturingLogger:
    """Lightweight logger capturing log records for assertions."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def _record(
        self,
        level: str,
        message: str,
        *args: Any,
        extra: Mapping[str, Any] | None = None,
        **_: Any,
    ) -> None:
        if args:
            try:
                message = message % args
            except Exception:
                try:
                    message = message.format(*args)
                except Exception:
                    pass
        payload = {
            "level": level,
            "message": message,
            "extra": dict(extra or {}),
        }
        self.records.append(payload)

    def debug(self, message: str, *args: Any, **kwargs: Any) -> None:
        self._record("debug", message, *args, **kwargs)

    def info(self, message: str, *args: Any, **kwargs: Any) -> None:
        self._record("info", message, *args, **kwargs)

    def warning(self, message: str, *args: Any, **kwargs: Any) -> None:
        self._record("warning", message, *args, **kwargs)

    def error(self, message: str, *args: Any, **kwargs: Any) -> None:
        self._record("error", message, *args, **kwargs)


class FakePipeline:
    def __init__(self, redis: "FakeRedis") -> None:
        self._redis = redis
        self._operations: list[tuple[str, str]] = []

    def set(self, key: str, value: str) -> "FakePipeline":
        self._operations.append((key, value))
        return self

    async def execute(self) -> list[bool]:
        results: list[bool] = []
        for key, value in self._operations:
            results.append(bool(await self._redis.set(key, value)))
        self._operations.clear()
        return results


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = defaultdict(dict)
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = defaultdict(list)
        self.published: list[tuple[str, str]] = []
        self.acked: list[tuple[str, str]] = []
        self.groups: set[tuple[str, str]] = set()
        self._counter = 0

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str) -> bool:
        self.store[str(key)] = str(value)
        return True

    def pipeline(self) -> FakePipeline:
        return FakePipeline(self)

    async def publish(self, channel: str, message: str) -> int:
        self.published.append((channel, message))
        return 1

    async def hset(self, key: str, *, mapping: Mapping[str, Any]) -> int:
        bucket = self.hashes[str(key)]
        for field, value in mapping.items():
            bucket[str(field)] = "" if value is None else str(value)
        return len(mapping)

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(str(key), {}))

    async def hdel(self, key: str, *fields: str) -> int:
        bucket = self.hashes.get(str(key))
        if not bucket:
            return 0
        removed = 0
        for field in fields:
            if field in bucket:
                del bucket[field]
                removed += 1
        return removed

    async def xadd(
        self,
        key: str,
        fields: Mapping[str, Any],
        *,
        maxlen: int | None = None,
        approximate: bool | None = None,
    ) -> str:
        stream = self.streams[str(key)]
        entry_id = f"{int(self._counter)}-0"
        self._counter += 1
        stream.append((entry_id, {str(k): str(v) for k, v in fields.items()}))
        if maxlen is not None and len(stream) > int(maxlen):
            stream[:] = stream[-int(maxlen) :]
        return entry_id

    async def xlen(self, key: str) -> int:
        return len(self.streams.get(str(key), []))

    async def xrevrange(self, key: str, *, count: int | None = None) -> list[tuple[str, Mapping[str, str]]]:
        stream = list(reversed(self.streams.get(str(key), [])))
        if count is not None:
            return stream[: int(count)]
        return stream

    async def xack(self, stream_key: str, group: str, *message_ids: str) -> int:
        for message_id in message_ids:
            self.acked.append((stream_key, str(message_id)))
        return len(message_ids)

    async def xgroup_create(self, stream: str, group: str, *, id: str = "$", mkstream: bool = False) -> bool:
        self.groups.add((stream, group))
        if mkstream:
            self.streams.setdefault(stream, [])
        return True

    async def xreadgroup(
        self,
        *,
        groupname: str,
        consumername: str,
        streams: Mapping[str, str],
        count: int | None = None,
        block: int | None = None,
    ) -> list[tuple[str, list[tuple[str, Mapping[str, str]]]]]:
        result: list[tuple[str, list[tuple[str, Mapping[str, str]]]]] = []
        for stream_key, _ in streams.items():
            entries = self.streams.get(str(stream_key), [])
            if not entries:
                continue
            items: list[tuple[str, Mapping[str, str]]] = []
            for message_id, data in entries:
                items.append((message_id, dict(data)))
                if count is not None and len(items) >= int(count):
                    break
            if items:
                result.append((str(stream_key), items))
        return result

