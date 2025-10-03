from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Mapping

from fastapi.testclient import TestClient

from services.notification.main import build_channels, create_app
from services.notification.service import NotificationChannel, NotificationEvent, NotificationService
from src.ibbot.redis import StreamMessage


def run(coro):
    return asyncio.run(coro)


class FakeStreamReader:
    def __init__(self, batches: list[list[StreamMessage]]) -> None:
        self._batches = batches

    async def read(self, _: dict[str, str]) -> list[StreamMessage]:
        if self._batches:
            return self._batches.pop(0)
        return []


class RecordingChannel:
    def __init__(self, name: str) -> None:
        self.name = name
        self.sent: list[tuple[NotificationEvent, str]] = []

    async def __call__(self, event: NotificationEvent, message: str) -> None:
        self.sent.append((event, message))


class FlakyChannel(RecordingChannel):
    def __init__(self, name: str, failures: int) -> None:
        super().__init__(name)
        self._remaining_failures = failures

    async def __call__(self, event: NotificationEvent, message: str) -> None:
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise RuntimeError(f"channel {self.name} failed")
        await super().__call__(event, message)


class DummyRedis:
    def __init__(self) -> None:
        self.entries: list[str] = []

    async def lpush(self, _key: str, value: str) -> int:
        self.entries.insert(0, value)
        return len(self.entries)

    async def ltrim(self, _key: str, _start: int, _stop: int) -> int:
        return len(self.entries)


class RecordingWriter:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def record_event(self, payload: Mapping[str, Any]) -> None:
        self.events.append(dict(payload))


def test_build_channels_respects_config() -> None:
    config = {"telegram_enabled": "true", "email_enabled": "0", "websocket_enabled": "YES"}
    channels = build_channels(config, "ibbot.services.notification.test")
    names = {channel.name for channel in channels}
    assert names == {"telegram", "websocket"}


def test_notification_service_dispatch_success(caplog) -> None:
    caplog.set_level(logging.INFO, logger="ibbot.services.notification")

    event = StreamMessage(stream="stream:ib:results", message_id="1-1", data={"type": "order", "status": "filled"})
    channel = RecordingChannel("telegram")
    service = NotificationService(
        stream_reader=FakeStreamReader([[event]]),
        channels=[NotificationChannel(name="telegram", sender=channel)],
        poll_interval=0,
    )

    processed = run(service.poll_once())
    assert processed == 1
    assert channel.sent[0][0].stream == "stream:ib:results"
    assert channel.sent[0][0].payload["status"] == "filled"

    success_records = [
        record
        for record in caplog.records
        if getattr(record, "notification_status", "") == "success"
        and getattr(record, "notification_channel", "") == "telegram"
    ]
    assert success_records, "expected success log entry"
    assert service.health_status["healthy"] is True


def test_notification_service_retry_flow(caplog) -> None:
    caplog.set_level(logging.INFO, logger="ibbot.services.notification")

    event = StreamMessage(stream="stream:risk:events", message_id="2-1", data={"type": "alert", "level": "high"})
    flaky = FlakyChannel("email", failures=1)
    service = NotificationService(
        stream_reader=FakeStreamReader([[event], []]),
        channels=[NotificationChannel(name="email", sender=flaky)],
        poll_interval=0,
        max_retries=3,
    )

    processed = run(service.poll_once())
    assert processed == 1
    assert service.get_retry_count("stream:risk:events", "2-1", "email") == 1
    assert service.health_status["healthy"] is False

    failure_records = [
        record
        for record in caplog.records
        if getattr(record, "notification_status", "") == "failed"
        and getattr(record, "notification_channel", "") == "email"
    ]
    assert failure_records, "expected failure log entry"

    caplog.clear()
    processed = run(service.poll_once())
    assert processed == 1
    assert service.get_retry_count("stream:risk:events", "2-1", "email") == 0
    assert service.health_status["healthy"] is True

    retry_success = [
        record
        for record in caplog.records
        if getattr(record, "notification_status", "") == "success"
        and getattr(record, "notification_channel", "") == "email"
    ]
    assert retry_success, "expected retry success log entry"


def test_notification_history_writer_payload() -> None:
    redis = DummyRedis()
    writer = RecordingWriter()

    async def writer_factory() -> RecordingWriter:
        return writer

    service = NotificationService(
        stream_reader=FakeStreamReader([]),
        channels=[],
        poll_interval=0,
        redis_client=redis,
        history_writer_factory=writer_factory,
    )

    event = NotificationEvent(
        stream="stream:alerts",
        message_id="1-1",
        payload={
            "command_id": "cmd-1",
            "user": "alice",
            "role": "operator",
            "category": "alerts",
        },
    )
    message = service._format_event(event)

    run(
        service._record_history(
            event=event,
            channel="telegram",
            status="success",
            message=message,
            retries=0,
        )
    )

    assert writer.events, "expected audit writer to capture entry"
    recorded = writer.events[0]
    assert recorded["action"] == "notification:telegram"
    assert recorded["result"] == "success"
    assert recorded["user"] == "alice"
    assert recorded["role"] == "operator"
    assert recorded["path"] == event.stream
    assert recorded["detail"] == message
    assert recorded["extra"]["command_id"] == "cmd-1"
    assert recorded["extra"]["status"] == "success"


def test_health_endpoint_uses_service_state() -> None:
    class StubService:
        def __init__(self) -> None:
            self.health_status = {"healthy": True, "service": "notification"}
            self._stopped = False

        async def run_forever(self) -> None:
            await asyncio.sleep(0)

        def stop(self) -> None:
            self._stopped = True

    stub = StubService()

    async def factory() -> tuple[StubService, Callable[[], Awaitable[None]] | None]:
        async def cleanup() -> None:  # pragma: no cover - no-op for tests
            stub.stop()

        return stub, cleanup

    app = create_app(service_factory=factory)
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["healthy"] is True
