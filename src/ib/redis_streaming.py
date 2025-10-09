from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import orjson
from pydantic import BaseModel
from redis.asyncio import Redis

from .events import (
    AccountUpdateEvent,
    DOMLevelEvent,
    ErrorEvent,
    ExecutionEvent,
    MarketTickEvent,
    OrderStatusEvent,
    PortfolioPositionEvent,
    RealTimeBarEvent,
    RedisChannel,
    TickByTickEvent,
)
from src.observability.metrics import record_redis_publish_failure

from loguru import logger


def _default_redis_url() -> str:
    return os.getenv("REDIS_URL", "redis://localhost:6379/0")


def _decimal_to_string(value: Decimal) -> str:
    try:
        if value.is_nan():
            return "NaN"
        if value.is_infinite():
            return "Infinity" if value > 0 else "-Infinity"
    except InvalidOperation:
        return str(value)
    return format(value, "f")


def _coerce_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _decimal_to_string(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {k: _coerce_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_coerce_value(v) for v in value]
    return value


class RedisPublisher:
    """Helper responsible for JSON serialization and pub/sub fan-out."""

    def __init__(
        self,
        *,
        redis: Redis | None = None,
        redis_url: str | None = None,
        max_retries: int = 3,
        backoff_base: float = 0.1,
        service_name: str = "core",
    ) -> None:
        self._redis = redis
        self._redis_url = redis_url or _default_redis_url()
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._lock = asyncio.Lock()
        self._service_name = service_name

    async def _ensure_connection(self) -> Redis:
        if self._redis is not None:
            return self._redis
        async with self._lock:
            if self._redis is None:
                from redis.asyncio import from_url

                self._redis = from_url(self._redis_url)
        if self._redis is None:
            raise RuntimeError("Failed to initialise Redis connection")
        return self._redis

    async def publish(self, channel: str, payload: BaseModel | Mapping[str, Any]) -> None:
        message = self._prepare_payload(payload)
        attempt = 0
        delay = self._backoff_base
        last_error: Exception | None = None
        while attempt <= self._max_retries:
            try:
                redis = await self._ensure_connection()
                await redis.publish(channel, message)
                return
            except Exception as exc:  # pragma: no cover - retry path
                last_error = exc
                record_redis_publish_failure(service=self._service_name)
                logger.warning(
                    "Redis publish failure on %s (attempt %s/%s): %s",
                    channel,
                    attempt + 1,
                    self._max_retries + 1,
                    exc,
                )
                if attempt == self._max_retries:
                    break
                await asyncio.sleep(delay)
                delay *= 2
                attempt += 1
        raise RuntimeError("Failed to publish to Redis") from last_error

    def _prepare_payload(self, payload: BaseModel | Mapping[str, Any]) -> bytes:
        if isinstance(payload, BaseModel):
            raw: Mapping[str, Any] = payload.model_dump()
        else:
            raw = payload
        serializable = {k: _coerce_value(v) for k, v in raw.items()}
        return orjson.dumps(serializable)

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.close()
            self._redis = None


async def publish_account_event(publisher: RedisPublisher, event: AccountUpdateEvent) -> None:
    await publisher.publish(RedisChannel.ACCOUNT_UPDATES.value, event)


async def publish_portfolio_event(publisher: RedisPublisher, event: PortfolioPositionEvent) -> None:
    await publisher.publish(RedisChannel.PORTFOLIO.value, event)


async def publish_order_event(publisher: RedisPublisher, event: OrderStatusEvent) -> None:
    await publisher.publish(RedisChannel.ORDERS.value, event)


async def publish_execution_event(publisher: RedisPublisher, event: ExecutionEvent) -> None:
    await publisher.publish(RedisChannel.EXECUTIONS.value, event)


async def publish_market_tick_event(publisher: RedisPublisher, event: MarketTickEvent) -> None:
    await publisher.publish(RedisChannel.MARKET_TICKS.value, event)


async def publish_dom_event(publisher: RedisPublisher, event: DOMLevelEvent) -> None:
    await publisher.publish(RedisChannel.MARKET_DOM.value, event)


async def publish_tick_by_tick_event(
    publisher: RedisPublisher, event: TickByTickEvent
) -> None:
    await publisher.publish(RedisChannel.MARKET_TICK_BY_TICK.value, event)


async def publish_real_time_bar_event(
    publisher: RedisPublisher, event: RealTimeBarEvent
) -> None:
    await publisher.publish(RedisChannel.MARKET_REAL_TIME_BARS.value, event)


async def publish_error_event(publisher: RedisPublisher, event: ErrorEvent) -> None:
    await publisher.publish(RedisChannel.ERRORS.value, event)
