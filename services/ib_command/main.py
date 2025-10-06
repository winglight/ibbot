"""Entry point for the IB command service."""

from __future__ import annotations

import asyncio
import os
import signal
from contextlib import AsyncExitStack
from typing import Any

from services.ib_command import CommandConsumer, IBCommandExecutor, ResultPublisher
from src.ibbot.config import ConfigProvider
from src.ibbot.logging import get_logger
from src.ibbot.redis import (
    DEFAULT_COMMAND_QUEUE,
    DEFAULT_REDIS_URL,
    close_global_client,
    get_redis_client,
)

SERVICE_NAME = "ib-command"
HEALTH_KEY = "hash:service:health:ib-command"


def _resolve_redis_url(override_key: str) -> str:
    for key in (override_key, "IBBOT_REDIS_URL"):
        value = os.getenv(key, "").strip()
        if value:
            return value
    return DEFAULT_REDIS_URL


async def _create_ib_client() -> Any:
    """Instantiate the IB client if available."""

    try:
        from src.ib_client_registry import get_client  # type: ignore
    except Exception:  # pragma: no cover - optional dependency
        return object()

    return await get_client("primary")


async def run_service() -> None:
    """Initialise dependencies and start the consumer loop."""

    logger = get_logger("ibbot.services.ib_command")
    redis_url = _resolve_redis_url("IB_COMMAND_REDIS_URL")
    redis = await get_redis_client(redis_url)

    provider = ConfigProvider(redis_url=redis_url)
    await provider.start()
    config = await provider.get_module(SERVICE_NAME)

    poll_timeout = float(config.get("poll_timeout", 1.0))
    retry_delay = float(config.get("retry_delay", 1.0))
    default_max_retries = int(config.get("max_retries", 3))
    queue_key = config.get("queue_key", DEFAULT_COMMAND_QUEUE)
    stream_key = config.get("stream_key", "stream:ib:results")
    hash_prefix = config.get("result_hash_prefix", "hash:ib:result")

    ib_client = await _create_ib_client()
    executor = IBCommandExecutor(ib_client)
    publisher = ResultPublisher(
        redis,
        stream_key=stream_key,
        hash_prefix=hash_prefix,
    )
    consumer = CommandConsumer(
        redis,
        executor,
        publisher,
        queue_key=queue_key,
        poll_timeout=poll_timeout,
        default_max_retries=default_max_retries,
        retry_delay=retry_delay,
        health_key=HEALTH_KEY,
    )

    stop_event = asyncio.Event()

    def _handle_stop(*_: Any) -> None:
        logger.info("Received stop signal; shutting down consumer")
        consumer.stop()
        stop_event.set()

    loop = asyncio.get_running_loop()
    for signame in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signame, _handle_stop)
        except NotImplementedError:  # pragma: no cover - platform dependent
            signal.signal(signame, lambda *_: asyncio.create_task(stop_event.set()))

    async with AsyncExitStack() as stack:
        stack.push_async_callback(provider.close)
        stack.push_async_callback(close_global_client)
        consumer_task = asyncio.create_task(consumer.consume_forever())
        stack.callback(consumer_task.cancel)
        await stop_event.wait()
        await consumer_task


def main() -> None:
    """Run the service until interrupted."""

    asyncio.run(run_service())


if __name__ == "__main__":  # pragma: no cover - manual execution path
    main()
