"""In-process event bus used by the unified data layer."""

from __future__ import annotations

import asyncio
import fnmatch
import inspect
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Iterable, List, MutableMapping, Optional

from loguru import logger

__all__ = [
    "BusSubscriptionToken",
    "EventEnvelope",
    "EventBus",
]

EventHandler = Callable[["EventEnvelope"], Optional[Awaitable[None]]]


@dataclass(frozen=True)
class BusSubscriptionToken:
    """Handle returned by :meth:`EventBus.subscribe`."""

    token: str
    topic_pattern: str


@dataclass
class EventEnvelope:
    """Standard event payload emitted by the data layer."""

    event_type: str
    topic: str
    symbol: Optional[str] = None
    source: Optional[str] = None
    ts_ns: Optional[int] = None
    sequence: Optional[int] = None
    payload: MutableMapping[str, Any] = field(default_factory=dict)
    meta: MutableMapping[str, Any] = field(default_factory=dict)
    correlation_id: Optional[str] = None

    def copy(self) -> "EventEnvelope":
        return EventEnvelope(
            event_type=self.event_type,
            topic=self.topic,
            symbol=self.symbol,
            source=self.source,
            ts_ns=self.ts_ns,
            sequence=self.sequence,
            payload=dict(self.payload),
            meta=dict(self.meta),
            correlation_id=self.correlation_id,
        )


class EventBus:
    """Simple publish/subscribe bus with wildcard topic support."""

    def __init__(self) -> None:
        self._subscribers: Dict[str, tuple[str, EventHandler]] = {}
        self._lock = threading.Lock()

    def subscribe(self, topic_pattern: str, handler: EventHandler) -> BusSubscriptionToken:
        """Register *handler* for events matching *topic_pattern*."""

        if not callable(handler):
            raise TypeError("handler must be callable")

        token = BusSubscriptionToken(token=str(uuid.uuid4()), topic_pattern=topic_pattern)
        with self._lock:
            self._subscribers[token.token] = (topic_pattern, handler)
        logger.debug("EventBus subscribed token=%s pattern=%s", token.token, topic_pattern)
        return token

    def unsubscribe(self, token: BusSubscriptionToken) -> None:
        """Remove a previously registered subscription."""

        with self._lock:
            removed = self._subscribers.pop(token.token, None)
        if removed:
            logger.debug("EventBus unsubscribed token=%s pattern=%s", token.token, token.topic_pattern)

    async def publish(self, event: EventEnvelope) -> None:
        """Publish *event* to all matching subscribers."""

        callbacks = self._match_handlers(event.topic)
        if not callbacks:
            return

        for handler in callbacks:
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.exception("EventBus handler failed: %s", exc)

    def publish_nowait(self, event: EventEnvelope) -> None:
        """Fire-and-forget variant of :meth:`publish`."""

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:  # pragma: no cover - defensive guard
            raise RuntimeError("publish_nowait requires an active event loop") from exc
        loop.create_task(self.publish(event))

    def _match_handlers(self, topic: str) -> List[EventHandler]:
        with self._lock:
            subscribers_snapshot = list(self._subscribers.values())

        matched: List[EventHandler] = []
        for pattern, handler in subscribers_snapshot:
            if fnmatch.fnmatch(topic, pattern):
                matched.append(handler)
        return matched


