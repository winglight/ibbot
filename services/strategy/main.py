"""Entry point for the strategy execution service."""

from __future__ import annotations

import asyncio
import json
import os
import signal
from contextlib import AsyncExitStack, suppress
from typing import Any, Iterable, Mapping

from services.strategy import StrategyAdapter, StrategyTaskConsumer
from src.ibbot import update_service_health
from src.ibbot.config import ConfigProvider
from src.ibbot.logging import configure_service_logger
from src.ibbot.redis import (
    DEFAULT_COMMAND_QUEUE,
    DEFAULT_REDIS_URL,
    close_global_client,
    get_redis_client,
)
from src.db.repositories import StrategyRepository, StrategyRepositoryError

SERVICE_NAME = "strategy"
DEFAULT_STRATEGY_QUEUE = "list:queue:strategy"
DEFAULT_EVENT_STREAM = "stream:strategy:events"


def _resolve_redis_url(override_key: str) -> str:
    for key in (override_key, "IBBOT_REDIS_URL"):
        value = os.getenv(key, "").strip()
        if value:
            return value
    return DEFAULT_REDIS_URL


def _parse_required_channels(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    payload = value
    if isinstance(payload, str):
        stripped = payload.strip()
        if not stripped:
            return ()
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            entries = [segment.strip() for segment in stripped.split(",") if segment.strip()]
            payload = entries
    if isinstance(payload, Iterable) and not isinstance(payload, (str, bytes)):
        result: list[str] = []
        seen: set[str] = set()
        for item in payload:
            if not isinstance(item, (str, bytes)):
                continue
            channel = str(item).strip()
            if not channel or channel in seen:
                continue
            seen.add(channel)
            result.append(channel)
        return tuple(result)
    return ()


def _parse_channel_map(value: Any) -> dict[str, dict[str, str]]:
    if not value:
        return {}
    payload = value
    if isinstance(payload, str):
        stripped = payload.strip()
        if not stripped:
            return {}
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return {}
    if isinstance(payload, Mapping):
        result: dict[str, dict[str, str]] = {}
        for symbol, mapping in payload.items():
            if not isinstance(mapping, Mapping):
                continue
            result[str(symbol)] = {
                str(kind): str(channel)
                for kind, channel in mapping.items()
                if isinstance(channel, (str, bytes)) and str(channel).strip()
            }
        return result
    return {}


def _parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    return None


def _parse_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return float(stripped)
        except ValueError:
            return None
    return None


async def _load_repository_strategies(
    repository: StrategyRepository,
    logger: Any,
) -> dict[str, Any]:
    try:
        records = await repository.list_strategies()
    except StrategyRepositoryError as exc:
        logger.error("Failed to load strategies from repository: %s", exc)
        return {}
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Unexpected repository failure while loading strategies: %s", exc)
        return {}

    strategies: dict[str, Any] = {}
    for record in records:
        if not record.enabled:
            continue
        strategy_type = record.strategy_type
        if not strategy_type:
            logger.warning(
                "Strategy %s skipped: repository entry missing strategy_type", record.name
            )
            continue
        strategies[record.name] = {
            "strategy_type": strategy_type,
            "params": dict(record.params),
        }
    return strategies


async def run_service() -> None:
    """Initialise dependencies and start the consumer loop."""

    logger = configure_service_logger(
        "ibbot.services.strategy",
        service_name="strategy",
        ib_client_id_env_var="STRATEGY_IB_CLIENT_ID",
    )

    try:
        from src.db.bootstrap import SchemaBootstrapError, ensure_schema
    except ImportError:  # pragma: no cover - optional dependency in minimal builds
        logger.debug("Database bootstrap helper unavailable; skipping schema check")
    else:
        try:
            await asyncio.to_thread(ensure_schema, logger=logger)
        except SchemaBootstrapError as exc:
            logger.exception("Database schema bootstrap failed: {}", exc)
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.warning("Database schema bootstrap skipped: {}", exc)

    redis_url = _resolve_redis_url("STRATEGY_REDIS_URL")
    redis = await get_redis_client(redis_url)

    provider = ConfigProvider(redis_url=redis_url)
    await provider.start()
    config = await provider.get_module(SERVICE_NAME)

    queue_key = config.get("queue_key", DEFAULT_STRATEGY_QUEUE)
    command_queue = config.get("command_queue", DEFAULT_COMMAND_QUEUE)
    event_stream = config.get("event_stream", DEFAULT_EVENT_STREAM)
    snapshot_prefix = config.get("snapshot_prefix", "hash:market:snapshot")
    poll_timeout = float(config.get("poll_timeout", 1.0))
    default_required_channels = _parse_required_channels(
        config.get("default_required_channels")
        or os.getenv("STRATEGY_DEFAULT_REQUIRED_CHANNELS")
    )
    default_channel_map = _parse_channel_map(
        config.get("default_channel_map")
        or os.getenv("STRATEGY_DEFAULT_CHANNEL_MAP")
    )
    default_latency_budget_ms = _parse_float(
        config.get("default_latency_budget_ms")
        or os.getenv("STRATEGY_DEFAULT_LATENCY_BUDGET_MS")
    )
    delayed_flag = config.get("use_delayed_data")
    if delayed_flag is None:
        delayed_flag = os.getenv("STRATEGY_USE_DELAYED_DATA")
    use_delayed_data = _parse_bool(delayed_flag)

    repository = StrategyRepository()
    strategies = await _load_repository_strategies(repository, logger)
    consumer: StrategyTaskConsumer | None = None
    consumer_task: asyncio.Task[None] | None = None
    idle_health_task: asyncio.Task[None] | None = None

    if not strategies:
        logger.info("No strategies configured; strategy service will idle until shutdown")
    else:
        adapter = StrategyAdapter(
            redis,
            strategies=strategies,
            command_queue=command_queue,
            event_stream=event_stream,
            snapshot_prefix=snapshot_prefix,
            default_required_channels=default_required_channels,
            default_channel_map=default_channel_map,
            default_latency_budget_ms=default_latency_budget_ms,
            use_delayed_data=use_delayed_data,
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
        if consumer is not None:
            consumer.stop()
        stop_event.set()

    loop = asyncio.get_running_loop()
    for signame in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signame, _handle_stop)
        except NotImplementedError:  # pragma: no cover - platform dependent
            signal.signal(signame, lambda *_: asyncio.create_task(stop_event.set()))

    async def _publish_idle_health() -> None:
        assert redis is not None
        interval = poll_timeout if poll_timeout > 0 else 1.0
        while True:
            try:
                await update_service_health(
                    redis,
                    SERVICE_NAME,
                    healthy=True,
                    metrics={"state": "idle", "queue": queue_key},
                    logger=logger,
                )
            except asyncio.CancelledError:  # pragma: no cover - cancellation path
                raise
            except Exception:  # pragma: no cover - defensive guard
                logger.warning("Failed to publish idle strategy health", exc_info=True)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                continue

    await update_service_health(
        redis,
        SERVICE_NAME,
        healthy=True,
        metrics={"state": "starting", "queue": queue_key},
        logger=logger,
    )

    async with AsyncExitStack() as stack:
        stack.push_async_callback(provider.close)
        stack.push_async_callback(close_global_client)
        stack.push_async_callback(repository.aclose)
        if consumer is None:
            idle_health_task = asyncio.create_task(_publish_idle_health())
            stack.callback(idle_health_task.cancel)
        if consumer is not None:
            consumer_task = asyncio.create_task(consumer.run_forever())
            stack.callback(consumer_task.cancel)
        await stop_event.wait()
        if consumer_task is not None:
            await consumer_task
        if idle_health_task is not None:
            with suppress(asyncio.CancelledError):
                await idle_health_task
        await update_service_health(
            redis,
            SERVICE_NAME,
            healthy=False,
            metrics={"state": "stopped", "queue": queue_key},
            logger=logger,
        )


def main() -> None:
    """Run the service until interrupted."""

    asyncio.run(run_service())


if __name__ == "__main__":  # pragma: no cover - manual execution path
    main()
