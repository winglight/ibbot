"""Entry point for the notification dispatch service."""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, HTTPException

from services.notification import NotificationChannel, NotificationService, RedisStreamReader
from src.ibbot.config import ConfigProvider
from src.ibbot.influx import write_points
from src.ibbot.logging import get_logger
from src.ibbot.redis import close_global_client, get_redis_client

SERVICE_NAME = "notification"
DEFAULT_STREAMS = ["stream:ib:results", "stream:risk:events", "stream:strategy:events"]

CleanupCallback = Callable[[], Awaitable[None] | None] | None
ServiceFactory = Callable[[], Awaitable[tuple[NotificationService, CleanupCallback]]]


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
    provider = ConfigProvider(redis_url=os.getenv("IBBOT_REDIS_URL"))
    await provider.start()
    config = await provider.get_module(SERVICE_NAME)

    redis = await get_redis_client()

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

    service = NotificationService(
        stream_reader=reader,
        channels=channels,
        poll_interval=poll_interval,
        max_retries=max_retries,
        write=write_points,
        service_name=SERVICE_NAME,
        logger_instance=logger,
    )

    async def _cleanup() -> None:
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
        if cleanup is not None:
            result = cleanup()
            if asyncio.iscoroutine(result):
                await result

    @app.get("/health")
    async def health() -> Any:
        service = getattr(app.state, "service", None)
        if service is None:
            raise HTTPException(status_code=503, detail="Notification service not initialised")
        return service.health_status

    return app


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
