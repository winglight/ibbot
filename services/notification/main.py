"""Entry point for the notification dispatch service."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import uuid
import inspect
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from services.notification import NotificationChannel, NotificationService, RedisStreamReader
from src.ibbot.config import ConfigProvider
from src.ibbot.logging import get_logger
from src.ibbot.redis import DEFAULT_REDIS_URL, close_global_client, get_redis_client
from src.notification_history import (
    DEFAULT_HISTORY_KEY,
    DEFAULT_HISTORY_LIMIT,
    InvalidHistoryLimitError,
    InvalidHistoryTimestampError,
    clear_notification_history,
    fetch_notification_history,
)

SERVICE_NAME = "notification"
DEFAULT_STREAMS = ["stream:ib:results", "stream:risk:events", "stream:strategy:events"]
DEFAULT_COMMAND_QUEUE = "queue:notification:dispatch"
DEFAULT_STATUS_PREFIX = "hash:notification:command"

CleanupCallback = Callable[[], Awaitable[None] | None] | None
ServiceFactory = Callable[[], Awaitable[tuple[NotificationService, CleanupCallback]]]


class NotificationDispatchRequest(BaseModel):
    """Payload accepted by the dispatch endpoint."""

    message: str = Field(..., description="Notification message to deliver")
    event_type: str | None = Field(None, description="Logical event type")
    category: str | None = Field(None, description="Notification category")
    channel: str | None = Field(None, description="Single channel name override")
    channels: list[str] | None = Field(None, description="Explicit channel list")
    metadata: Mapping[str, Any] | None = Field(None, description="Additional metadata")

    model_config = ConfigDict(extra="allow")


class NotificationTestRequest(NotificationDispatchRequest):
    """Payload accepted by the test endpoint."""

    message: str | None = Field(None, description="Optional test message override")


def _resolve_redis_url(override_key: str) -> str:
    for key in (override_key, "IBBOT_REDIS_URL"):
        value = os.getenv(key, "").strip()
        if value:
            return value
    return DEFAULT_REDIS_URL


def _is_enabled(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}


def build_channels(config: Any, logger_name: str) -> list[NotificationChannel]:
    logger = get_logger(logger_name)

    def _logging_sender(channel_name: str) -> Callable[[Any, str], Awaitable[None] | None]:
        async def _sender(event: Any, message: str) -> None:
            logger.info(
                "Dispatching notification",
                extra={
                    "channel": channel_name,
                    "stream": getattr(event, "stream", "unknown"),
                    "message_id": getattr(event, "message_id", "unknown"),
                },
            )
            logger.debug("Notification payload", extra={"channel": channel_name, "payload": message})

        return _sender

    channels: list[NotificationChannel] = []
    if _is_enabled(config.get("telegram_enabled")):
        channels.append(NotificationChannel(name="telegram", sender=_logging_sender("telegram")))
    if _is_enabled(config.get("email_enabled")):
        channels.append(NotificationChannel(name="email", sender=_logging_sender("email")))
    if _is_enabled(config.get("websocket_enabled")):
        channels.append(NotificationChannel(name="websocket", sender=_logging_sender("websocket")))
    return channels


async def _default_service_factory() -> tuple[NotificationService, CleanupCallback]:
    logger = get_logger("ibbot.services.notification")
    redis_url = _resolve_redis_url("NOTIFICATION_REDIS_URL")
    provider = ConfigProvider(redis_url=redis_url)
    await provider.start()
    config = await provider.get_module(SERVICE_NAME)

    redis = await get_redis_client(redis_url)

    streams_config = config.get("streams")
    streams = DEFAULT_STREAMS
    if streams_config:
        candidate = [entry.strip() for entry in streams_config.split(",") if entry.strip()]
        if candidate:
            streams = candidate

    block_ms = int(config.get("stream_block_ms", 1000) or 1000)
    count_value = config.get("stream_count")
    count = int(count_value) if count_value else None

    reader = RedisStreamReader(redis, streams, block_ms=block_ms, count=count)
    channels = build_channels(config, "ibbot.services.notification")

    poll_interval = float(config.get("poll_interval", 1.0) or 1.0)
    max_retries = int(config.get("max_retries", 3) or 3)
    command_queue = str(config.get("command_queue") or DEFAULT_COMMAND_QUEUE)
    status_prefix = str(config.get("command_status_prefix") or DEFAULT_STATUS_PREFIX)
    batch_size_value = config.get("command_batch_size")
    command_batch_size = int(batch_size_value) if batch_size_value else 10

    history_key = str(config.get("history_key") or DEFAULT_HISTORY_KEY)
    history_max_entries_value = config.get("history_max_entries")
    history_max_entries = (
        int(history_max_entries_value)
        if history_max_entries_value
        else DEFAULT_HISTORY_LIMIT
    )

    history_writer_factory: Callable[[], Awaitable[Any]] | None = None
    try:
        from src.utils.audit import create_audit_log_writer
    except ModuleNotFoundError:
        pass
    else:

        async def _history_writer_factory() -> Any:
            writer = create_audit_log_writer()
            if inspect.isawaitable(writer):
                return await writer
            return writer

        history_writer_factory = _history_writer_factory

    service = NotificationService(
        stream_reader=reader,
        channels=channels,
        poll_interval=poll_interval,
        max_retries=max_retries,
        service_name=SERVICE_NAME,
        logger_instance=logger,
        redis_client=redis,
        command_queue=command_queue,
        command_status_prefix=status_prefix,
        command_batch_size=command_batch_size,
        history_key=history_key,
        history_max_entries=history_max_entries,
        history_writer_factory=history_writer_factory,
    )

    async def _cleanup() -> None:
        await service.aclose()
        await provider.close()
        await close_global_client()

    return service, _cleanup


def create_app(service_factory: ServiceFactory | None = None) -> FastAPI:
    app = FastAPI(title="IBBot Notification Service", version="0.1.0")

    factory = service_factory or _default_service_factory

    @app.on_event("startup")
    async def _startup() -> None:
        service, cleanup = await factory()
        app.state.service = service
        app.state.cleanup = cleanup
        app.state.worker = asyncio.create_task(service.run_forever())
        redis = getattr(service, "redis", None)
        app.state.redis = redis
        app.state.command_queue = getattr(service, "command_queue", DEFAULT_COMMAND_QUEUE)
        app.state.command_status_prefix = getattr(service, "command_status_prefix", DEFAULT_STATUS_PREFIX)
        app.state.history_key = getattr(service, "history_key", DEFAULT_HISTORY_KEY)
        app.state.history_limit = getattr(
            service, "history_max_entries", DEFAULT_HISTORY_LIMIT
        )

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        worker = getattr(app.state, "worker", None)
        service = getattr(app.state, "service", None)
        cleanup = getattr(app.state, "cleanup", None)

        if service is not None:
            service.stop()
        if worker is not None:
            try:
                await worker
            except asyncio.CancelledError:  # pragma: no cover - cancellation path depends on runtime
                pass
        if service is not None:
            close_callable = getattr(service, "aclose", None)
            if callable(close_callable):
                result = close_callable()
                if asyncio.iscoroutine(result):
                    await result
        if cleanup is not None:
            result = cleanup()
            if asyncio.iscoroutine(result):
                await result
        app.state.redis = None

    @app.get("/health")
    async def health() -> Any:
        service = getattr(app.state, "service", None)
        if service is None:
            raise HTTPException(status_code=503, detail="Notification service not initialised")
        return service.health_status

    def _get_runtime_state() -> tuple[Any, str, str]:
        redis = getattr(app.state, "redis", None)
        if redis is None:
            raise HTTPException(status_code=503, detail="Notification runtime unavailable")
        queue_key = getattr(app.state, "command_queue", DEFAULT_COMMAND_QUEUE)
        status_prefix = getattr(app.state, "command_status_prefix", DEFAULT_STATUS_PREFIX)
        return redis, queue_key, status_prefix

    def _get_history_state() -> tuple[Any, str, int]:
        redis = getattr(app.state, "redis", None)
        if redis is None:
            raise HTTPException(status_code=503, detail="Notification runtime unavailable")
        history_key = getattr(app.state, "history_key", DEFAULT_HISTORY_KEY)
        history_limit = getattr(app.state, "history_limit", DEFAULT_HISTORY_LIMIT)
        return redis, history_key, history_limit

    def _normalise_channels(request: NotificationDispatchRequest) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()

        def _add(candidate: str | None) -> None:
            if not candidate:
                return
            name = candidate.strip().lower()
            if not name or name in seen:
                return
            seen.add(name)
            result.append(name)

        if request.channels:
            for entry in request.channels:
                _add(str(entry) if entry is not None else None)
        _add(request.channel)
        return result

    def _json_default(value: Any) -> Any:
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        if isinstance(value, Mapping):
            return dict(value)
        if isinstance(value, Iterable) and not isinstance(value, (str, bytes, bytearray)):
            return list(value)
        return str(value)

    async def _enqueue_command(command_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        redis, queue_key, status_prefix = _get_runtime_state()
        command_id = uuid.uuid4().hex
        envelope = {"command_id": command_id, "type": command_type, "payload": dict(payload)}
        status_key = f"{status_prefix.rstrip(':')}:{command_id}"
        timestamp = datetime.now(timezone.utc).isoformat()
        await redis.hset(status_key, mapping={"status": "queued", "type": command_type, "requested_at": timestamp})
        await redis.rpush(queue_key, json.dumps(envelope, ensure_ascii=False, default=_json_default))
        return {"success": True, "command_id": command_id, "status": "queued"}

    @app.post("/notifications/dispatch", status_code=status.HTTP_202_ACCEPTED)
    async def dispatch_notification(request: NotificationDispatchRequest) -> Mapping[str, Any]:
        channels = _normalise_channels(request)
        payload = request.model_dump(mode="python")
        if channels:
            payload["channels"] = channels
        result = await _enqueue_command("dispatch", payload)
        return result

    @app.post("/notifications/test", status_code=status.HTTP_202_ACCEPTED)
    async def test_notification(request: NotificationTestRequest) -> Mapping[str, Any]:
        payload = request.model_dump(mode="python")
        if not payload.get("message"):
            payload["message"] = "Test notification from IBBot"
        payload.setdefault("event_type", "test")
        payload.setdefault("category", "api")
        payload["test_mode"] = True
        channels = _normalise_channels(request)
        if channels:
            payload["channels"] = channels
        result = await _enqueue_command("test", payload)
        return result

    @app.get("/notifications/status")
    async def notification_status(command_id: str) -> Mapping[str, Any]:
        if not command_id:
            raise HTTPException(status_code=400, detail="command_id is required")
        redis, _, status_prefix = _get_runtime_state()
        status_key = f"{status_prefix.rstrip(':')}:{command_id}"
        record = await redis.hgetall(status_key)
        if not record:
            raise HTTPException(status_code=404, detail="Command not found")
        payload: dict[str, Any] = {str(key): value for key, value in record.items()}
        for field in ("results", "event"):
            raw = payload.get(field)
            if isinstance(raw, str):
                try:
                    payload[field] = json.loads(raw)
                except json.JSONDecodeError:
                    continue
        payload["command_id"] = command_id
        payload.setdefault("status", "unknown")
        return payload

    @app.get("/notifications/history")
    async def notification_history(
        channel: str | None = None,
        status: str | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> Mapping[str, Any]:
        redis, history_key, history_limit = _get_history_state()

        try:
            payload = await fetch_notification_history(
                redis,
                history_key=history_key,
                history_limit=history_limit,
                channel=channel,
                status=status,
                since=since,
                limit=limit,
            )
        except InvalidHistoryLimitError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except InvalidHistoryTimestampError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return payload

    async def _clear_history() -> Mapping[str, Any]:
        redis, history_key, _ = _get_history_state()
        return await clear_notification_history(redis, history_key=history_key)

    @app.delete("/notifications/history")
    async def delete_history() -> Mapping[str, Any]:
        return await _clear_history()

    @app.post("/notifications/history/clear")
    async def clear_history() -> Mapping[str, Any]:
        return await _clear_history()

    return app


app = create_app()


async def run_service() -> None:
    service, cleanup = await _default_service_factory()
    stop_event = asyncio.Event()

    async def _handle_shutdown() -> None:
        await stop_event.wait()
        service.stop()

    loop = asyncio.get_running_loop()

    for signame in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signame, stop_event.set)
        except (AttributeError, NotImplementedError):  # pragma: no cover - platform dependent
            pass

    worker = asyncio.create_task(service.run_forever())
    try:
        await _handle_shutdown()
    finally:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:  # pragma: no cover - cancellation path depends on runtime
            pass
        if cleanup is not None:
            result = cleanup()
            if asyncio.iscoroutine(result):
                await result


def main() -> None:
    asyncio.run(run_service())


if __name__ == "__main__":  # pragma: no cover - manual execution path
    main()
