"""Combined Interactive Brokers gateway service.

This service owns the single Interactive Brokers client instance and wires it
into the legacy command queue as well as the market-data publication pipeline.
"""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from typing import Any

from services.ib_command import CommandConsumer, IBCommandExecutor, ResultPublisher
from services.ib_feed import MarketDataFeedService
from src.db import ConfigRepositoryError
from src.ibbot.config import ConfigProvider
from src.ibbot.logging import get_logger
from src.ibbot.redis import (
    DEFAULT_COMMAND_QUEUE,
    DEFAULT_REDIS_URL,
    close_global_client,
    get_redis_client,
)

SERVICE_NAME = "ib-gateway"
COMMAND_HEALTH_KEY = "hash:service:health:ib-command"
FEED_HEALTH_KEY = "hash:service:health:ib-feed"


def _resolve_redis_url(env_override: str) -> str:
    for key in (env_override, "IBBOT_REDIS_URL"):
        value = os.getenv(key, "").strip()
        if value:
            return value
    return DEFAULT_REDIS_URL


async def _create_ib_client() -> Any:
    try:
        from src.ib_client_registry import get_client  # type: ignore
    except Exception:  # pragma: no cover - optional dependency in tests
        class _UnavailableClient:
            async def connect(self) -> None:  # pragma: no cover - defensive fallback
                raise RuntimeError("IBKR client is not available")

        return _UnavailableClient()

    return await get_client("gateway")


async def _load_module(provider: ConfigProvider, name: str) -> Mapping[str, str]:
    try:
        return await provider.get_module(name)
    except ConfigRepositoryError:
        return {}
    except Exception:  # pragma: no cover - defensive guard
        logger = get_logger("ibbot.services.ib_gateway.config")
        logger.warning("Failed to load config module", module=name, exc_info=True)
        return {}


def _coerce_sequence(value: Any) -> list[str]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [str(item) for item in value]
    if isinstance(value, str):
        return [value]
    return []


async def run_service() -> None:
    """Start the gateway service until interrupted."""

    logger = get_logger("ibbot.services.ib_gateway")
    redis_url = _resolve_redis_url("IB_GATEWAY_REDIS_URL")
    redis = await get_redis_client(redis_url)

    provider = ConfigProvider(redis_url=redis_url)
    await provider.start()

    gateway_config = await _load_module(provider, SERVICE_NAME)
    command_config = await _load_module(provider, "ib-command")
    feed_config = await _load_module(provider, "ib-feed")

    queue_key = str(
        gateway_config.get("queue_key")
        or command_config.get("queue_key")
        or DEFAULT_COMMAND_QUEUE
    )
    stream_key = str(
        gateway_config.get("stream_key")
        or command_config.get("stream_key")
        or "stream:ib:results"
    )
    hash_prefix = str(
        gateway_config.get("result_hash_prefix")
        or command_config.get("result_hash_prefix")
        or "hash:ib:result"
    )
    poll_timeout = float(
        gateway_config.get("poll_timeout")
        or command_config.get("poll_timeout")
        or 1.0
    )
    retry_delay = float(
        gateway_config.get("retry_delay")
        or command_config.get("retry_delay")
        or 1.0
    )
    default_max_retries = int(
        gateway_config.get("max_retries")
        or command_config.get("max_retries")
        or 3
    )

    subscriptions = _coerce_sequence(
        gateway_config.get("symbols") or feed_config.get("symbols") or []
    )
    reconnect_delay = float(
        gateway_config.get("reconnect_delay")
        or feed_config.get("reconnect_delay")
        or 1.0
    )

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
        health_key=COMMAND_HEALTH_KEY,
    )

    feed_service = MarketDataFeedService(
        ib_client,
        redis,
        subscriptions=subscriptions,
        reconnect_delay=reconnect_delay,
        health_service_name=SERVICE_NAME,
        health_key=FEED_HEALTH_KEY,
    )

    stop_event = asyncio.Event()

    def _handle_stop(*_: Any) -> None:
        logger.info("Received stop signal; shutting down IB gateway")
        consumer.stop()
        stop_event.set()

    loop = asyncio.get_running_loop()
    for signame in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signame, _handle_stop)
        except NotImplementedError:  # pragma: no cover - Windows support
            signal.signal(signame, lambda *_: asyncio.create_task(stop_event.set()))

    async with AsyncExitStack() as stack:
        stack.push_async_callback(provider.close)
        stack.push_async_callback(close_global_client)
        stack.push_async_callback(feed_service.stop)

        await feed_service.start()

        consumer_task = asyncio.create_task(consumer.consume_forever())
        stack.callback(consumer_task.cancel)

        await stop_event.wait()
        await consumer_task


def main() -> None:
    asyncio.run(run_service())


if __name__ == "__main__":  # pragma: no cover - manual entrypoint
    main()
