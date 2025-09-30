"""Entry point for the strategy execution service."""

from __future__ import annotations

import asyncio
import json
import os
import signal
from contextlib import AsyncExitStack
from typing import Any, Mapping

from services.strategy import StrategyAdapter, StrategyTaskConsumer
from src.ibbot.config import ConfigProvider
from src.ibbot.logging import get_logger
from src.ibbot.redis import DEFAULT_COMMAND_QUEUE, close_global_client, get_redis_client

SERVICE_NAME = "strategy"
DEFAULT_STRATEGY_QUEUE = "list:queue:strategy"
DEFAULT_EVENT_STREAM = "stream:strategy:events"


def _parse_strategies(config: Mapping[str, str]) -> Mapping[str, Any]:
    raw = config.get("strategies")
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid strategy configuration payload: {exc}") from exc
    if isinstance(raw, Mapping):
        return raw
    raise RuntimeError("strategies configuration must be JSON or mapping")


async def run_service() -> None:
    """Initialise dependencies and start the consumer loop."""

    logger = get_logger("ibbot.services.strategy")
    redis = await get_redis_client()

    provider = ConfigProvider(redis_url=os.getenv("IBBOT_REDIS_URL"))
    await provider.start()
    config = await provider.get_module(SERVICE_NAME)

    queue_key = config.get("queue_key", DEFAULT_STRATEGY_QUEUE)
    command_queue = config.get("command_queue", DEFAULT_COMMAND_QUEUE)
    event_stream = config.get("event_stream", DEFAULT_EVENT_STREAM)
    snapshot_prefix = config.get("snapshot_prefix", "hash:market:snapshot")
    poll_timeout = float(config.get("poll_timeout", 1.0))

    strategies = _parse_strategies(config)
    if not strategies:
        raise RuntimeError("no strategies defined in configuration")

    adapter = StrategyAdapter(
        redis,
        strategies=strategies,
        command_queue=command_queue,
        event_stream=event_stream,
        snapshot_prefix=snapshot_prefix,
    )
    consumer = StrategyTaskConsumer(
        redis,
        adapter,
        queue_key=queue_key,
        poll_timeout=poll_timeout,
    )

    stop_event = asyncio.Event()

    def _handle_stop(*_: Any) -> None:
        logger.info("Received stop signal; shutting down strategy consumer")
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
        consumer_task = asyncio.create_task(consumer.run_forever())
        stack.callback(consumer_task.cancel)
        await stop_event.wait()
        await consumer_task


def main() -> None:
    """Run the service until interrupted."""

    asyncio.run(run_service())


if __name__ == "__main__":  # pragma: no cover - manual execution path
    main()
