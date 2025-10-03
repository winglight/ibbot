from __future__ import annotations

import asyncio
import json
from typing import Any, Iterable, Mapping

from fastapi.testclient import TestClient

from services.notification import NotificationChannel
from services.notification.main import create_app
from services.notification.service import NotificationEvent, NotificationService


class FakeRedis:
    def __init__(self) -> None:
        self.queues: dict[str, list[str]] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.lists: dict[str, list[str]] = {}

    async def rpush(self, key: str, value: str) -> int:
        queue = self.queues.setdefault(key, [])
        queue.append(value)
        return len(queue)

    async def lpop(self, key: str) -> str | None:
        queue = self.queues.get(key)
        if not queue:
            return None
        return queue.pop(0)

    async def llen(self, key: str) -> int:
        return len(self.queues.get(key, []))

    async def hset(self, key: str, mapping: Mapping[str, Any]) -> int:
        record = self.hashes.setdefault(key, {})
        for field, value in mapping.items():
            record[str(field)] = str(value)
        return len(record)

    async def hgetall(self, key: str) -> Mapping[str, str]:
        return dict(self.hashes.get(key, {}))

    async def lpush(self, key: str, value: str) -> int:
        bucket = self.lists.setdefault(key, [])
        bucket.insert(0, value)
        return len(bucket)

    async def ltrim(self, key: str, start: int, stop: int) -> int:
        bucket = self.lists.get(key)
        if bucket is None:
            return 0
        length = len(bucket)
        if length == 0:
            return 0
        start = max(start, 0)
        if stop < 0:
            stop = length + stop
        stop = min(stop, length - 1)
        if start >= length or stop < start:
            self.lists[key] = []
        else:
            self.lists[key] = bucket[start : stop + 1]
        return len(self.lists[key])

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        bucket = self.lists.get(key, [])
        length = len(bucket)
        if length == 0:
            return []
        if start < 0:
            start = max(length + start, 0)
        if stop < 0:
            stop = length + stop
        stop = min(stop, length - 1)
        start = max(start, 0)
        if stop < start:
            return []
        return bucket[start : stop + 1]

    async def delete(self, key: str) -> int:
        removed = 1 if key in self.lists else 0
        self.lists.pop(key, None)
        return removed

    async def xread(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        return []


class FakeStreamReader:
    async def read(self, _: Mapping[str, str]) -> list[Any]:
        return []


class RecordingChannel:
    def __init__(self, name: str, *, fail_times: int = 0) -> None:
        self.name = name
        self.fail_times = fail_times
        self.sent: list[tuple[NotificationEvent, str]] = []

    async def __call__(self, event: NotificationEvent, message: str) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError(f"channel {self.name} failed")
        self.sent.append((event, message))


class IdleNotificationService(NotificationService):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._idle_event: asyncio.Event | None = None

    async def run_forever(self) -> None:  # pragma: no cover - exercised indirectly
        if self._idle_event is None:
            self._idle_event = asyncio.Event()
        await self._idle_event.wait()

    def stop(self) -> None:
        super().stop()
        if self._idle_event is not None:
            self._idle_event.set()


def _build_app(
    *,
    redis: FakeRedis,
    channels: Iterable[NotificationChannel],
) -> tuple[TestClient, IdleNotificationService]:
    service = IdleNotificationService(
        stream_reader=FakeStreamReader(),
        channels=list(channels),
        poll_interval=0.05,
        redis_client=redis,
        command_queue="queue:test:notification",
        command_status_prefix="hash:test:notification",
    )

    async def factory() -> tuple[IdleNotificationService, Any]:
        return service, None

    app = create_app(service_factory=factory)
    return TestClient(app), service


def _process_queue(service: IdleNotificationService) -> None:
    asyncio.run(service.poll_once())


def _fetch_status(client: TestClient, command_id: str) -> Mapping[str, Any]:
    response = client.get("/notifications/status", params={"command_id": command_id})
    assert response.status_code == 200
    payload = response.json()
    if isinstance(payload.get("results"), str):
        payload["results"] = json.loads(payload["results"])
    return payload


def test_dispatch_notification_success() -> None:
    redis = FakeRedis()
    channel = RecordingChannel("websocket")
    client, service = _build_app(redis=redis, channels=[NotificationChannel(name="websocket", sender=channel)])

    with client:
        response = client.post("/notifications/dispatch", json={"message": "Hello", "channels": ["websocket"]})
        assert response.status_code == 202
        command_id = response.json()["command_id"]

        _process_queue(service)

        status = _fetch_status(client, command_id)
        assert status["status"] == "success"
        assert status["results"]["websocket"]["status"] == "success"
        assert channel.sent and channel.sent[0][0].message_id == command_id


def test_dispatch_notification_failure_records_error() -> None:
    redis = FakeRedis()
    channel = RecordingChannel("telegram", fail_times=1)
    client, service = _build_app(redis=redis, channels=[NotificationChannel(name="telegram", sender=channel)])

    with client:
        response = client.post("/notifications/dispatch", json={"message": "Hello", "channels": ["telegram"]})
        assert response.status_code == 202
        command_id = response.json()["command_id"]

        _process_queue(service)

        status = _fetch_status(client, command_id)
        assert status["status"] == "failed"
        result = status["results"]["telegram"]
        assert result["status"] == "retrying"
        assert "error" in result
        assert "last_error" in status


def test_notification_status_missing_command_returns_404() -> None:
    redis = FakeRedis()
    client, _ = _build_app(redis=redis, channels=[])

    with client:
        response = client.get("/notifications/status", params={"command_id": "missing"})
        assert response.status_code == 404


def test_history_endpoint_returns_recent_entries() -> None:
    redis = FakeRedis()
    client, service = _build_app(redis=redis, channels=[])

    async def _populate() -> None:
        await redis.lpush(service.history_key, json.dumps({"channel": "websocket", "status": "success", "timestamp": "2024-06-01T00:00:00+00:00"}))
        await redis.lpush(service.history_key, json.dumps({"channel": "telegram", "status": "failed", "timestamp": "2024-06-02T00:00:00+00:00", "error": "boom"}))

    asyncio.run(_populate())

    with client:
        response = client.get(
            "/notifications/history",
            params={"channel": "telegram", "status": "failed", "limit": 5},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is True
        assert len(payload["history"]) == 1
        assert payload["history"][0]["channel"] == "telegram"


def test_clear_history_deletes_entries() -> None:
    redis = FakeRedis()
    client, service = _build_app(redis=redis, channels=[])

    async def _populate() -> None:
        await redis.lpush(service.history_key, json.dumps({"channel": "telegram", "status": "success"}))

    asyncio.run(_populate())

    with client:
        response = client.delete("/notifications/history")
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["cleared"] >= 0

        response = client.post("/notifications/history/clear")
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True

