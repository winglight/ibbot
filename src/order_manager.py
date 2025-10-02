"""Compatibility wrapper that proxies order operations to the orders service.

The legacy application used to persist orders inside the local SQLite
database and mirrored the records in memory so that web handlers could serve
snapshots without hitting the database on every request.  The new orders
micro-service owns that persistence layer.  ``OrderManager`` therefore only
needs to forward CRUD operations to :class:`OrdersServiceClient` and fall back
to queuing commands directly when the service is temporarily unavailable.

Keeping a very small compatibility layer here allows other parts of the code
base to keep calling ``OrderManager`` while we migrate the underlying storage.
The class mirrors the small surface that the UI relies on: placing and
canceling orders plus querying the service for snapshots and event history.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Awaitable, Callable, Mapping, MutableMapping

from loguru import logger

from src.ibbot.redis import DEFAULT_COMMAND_QUEUE, enqueue_ib_command, get_redis_client
from src.routes.service_clients import (
    OrdersServiceClient,
    ServiceClientError,
    ServiceUnavailableError,
)

orders_logger = logger.bind(module="order_manager", request_id="-")


class OrderManager:
    """Proxy class that delegates order operations to ``OrdersServiceClient``."""

    def __init__(
        self,
        *,
        client: OrdersServiceClient | None = None,
        redis_factory: Callable[[], Awaitable[Any]] | None = None,
        command_queue: str | None = None,
    ) -> None:
        self._client = client or OrdersServiceClient()
        self._redis_factory = redis_factory
        self._command_queue = (command_queue or DEFAULT_COMMAND_QUEUE).strip() or DEFAULT_COMMAND_QUEUE

        if self._redis_factory is None:
            # Lazily resolve Redis when a fallback is required.  Importing the
            # helper at module import time keeps ``__init__`` lightweight for
            # test environments where redis-py may not be installed.
            self._redis_factory = get_redis_client

    # ------------------------------------------------------------------
    def set_dependencies(
        self,
        *,
        redis_factory: Callable[[], Awaitable[Any]] | None = None,
        command_queue: str | None = None,
        **_: Any,
    ) -> None:
        """Compatibility hook retained for legacy initialisation paths."""

        if redis_factory is not None:
            self._redis_factory = redis_factory
        if command_queue:
            queue_key = command_queue.strip()
            if queue_key:
                self._command_queue = queue_key

    # ------------------------------------------------------------------
    async def list_orders(
        self,
        *,
        limit: int | None = None,
        offset: int | None = None,
        start_date: str | None = None,
    ) -> Any:
        return await self._client.list_orders(limit=limit, offset=offset, start_date=start_date)

    async def get_order(self, order_id: str) -> Any:
        return await self._client.get_order(order_id)

    async def get_order_events(self, order_id: str) -> Any:
        return await self._client.get_order_events(order_id)

    # ------------------------------------------------------------------
    async def submit_order(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            return await self._client.place_order(payload)
        except ServiceUnavailableError as exc:
            orders_logger.warning(
                "Orders service unavailable when placing order; falling back to Redis queue",
                error=str(exc),
            )
            return await self._enqueue_fallback("place_order", kwargs=dict(payload), original_error=exc)

    async def cancel_order(self, order_id: str) -> Mapping[str, Any]:
        try:
            return await self._client.cancel_order(order_id)
        except ServiceUnavailableError as exc:
            orders_logger.warning(
                "Orders service unavailable when cancelling order; falling back to Redis queue",
                order_id=order_id,
                error=str(exc),
            )
            return await self._enqueue_fallback(
                "cancel_order",
                kwargs={"order_id": order_id},
                original_error=exc,
            )

    async def cancel_all_orders(self) -> Mapping[str, Any]:
        try:
            return await self._client.cancel_all()
        except ServiceUnavailableError as exc:
            orders_logger.warning(
                "Orders service unavailable when cancelling all orders; falling back to Redis queue",
                error=str(exc),
            )
            return await self._enqueue_fallback("cancel_all_orders", kwargs={}, original_error=exc)

    async def close_position(self, symbol: str) -> Mapping[str, Any]:
        try:
            return await self._client.close_position(symbol)
        except ServiceUnavailableError as exc:
            orders_logger.warning(
                "Orders service unavailable when closing position; falling back to Redis queue",
                symbol=symbol,
                error=str(exc),
            )
            return await self._enqueue_fallback(
                "close_position",
                kwargs={"symbol": symbol},
                original_error=exc,
            )

    async def reverse_position(self, symbol: str) -> Mapping[str, Any]:
        try:
            return await self._client.reverse_position(symbol)
        except ServiceUnavailableError as exc:
            orders_logger.warning(
                "Orders service unavailable when reversing position; falling back to Redis queue",
                symbol=symbol,
                error=str(exc),
            )
            return await self._enqueue_fallback(
                "reverse_position",
                kwargs={"symbol": symbol},
                original_error=exc,
            )

    # ------------------------------------------------------------------
    async def _enqueue_fallback(
        self,
        method: str,
        *,
        kwargs: Mapping[str, Any],
        original_error: ServiceClientError,
    ) -> Mapping[str, Any]:
        """Enqueue an order command directly on the Redis queue fallback."""

        if self._redis_factory is None:
            raise original_error

        try:
            redis = await self._redis_factory()
        except Exception as exc:  # pragma: no cover - defensive guard
            orders_logger.error(
                "Redis fallback factory failed while handling orders service outage",
                error=str(exc),
            )
            raise original_error from exc

        if redis is None:  # pragma: no cover - guard against optional redis
            orders_logger.warning(
                "Redis fallback unavailable; propagating orders service error",
                method=method,
            )
            raise original_error

        command_id = uuid.uuid4().hex
        payload: MutableMapping[str, Any] = {
            "command_id": command_id,
            "method": method,
            "args": [],
            "kwargs": json.loads(json.dumps(dict(kwargs))),
        }

        try:
            await enqueue_ib_command(payload, redis=redis, queue_key=self._command_queue)
        except Exception as exc:  # pragma: no cover - depends on Redis backend
            orders_logger.error(
                "Failed to enqueue fallback order command",
                method=method,
                command_id=command_id,
                error=str(exc),
            )
            raise original_error from exc

        orders_logger.info(
            "Enqueued fallback order command",
            method=method,
            command_id=command_id,
            queue=self._command_queue,
        )
        return {
            "command_id": command_id,
            "queue": self._command_queue,
            "status": "queued",
            "fallback": True,
        }


__all__ = ["OrderManager"]

