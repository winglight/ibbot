"""Minimal fakeredis replacement used when the real dependency is unavailable."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List


@dataclass
class _Subscriber:
    channel: str
    queue: Deque[Any]
    closed: bool = False


class FakeServer:
    """Lightweight in-memory broker suitable for unit tests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: Dict[str, List[_Subscriber]] = defaultdict(list)

    def register(self, channel: str, subscriber: _Subscriber) -> None:
        with self._lock:
            subs = list(self._subscribers.get(channel, ()))
            subs.append(subscriber)
            self._subscribers[channel] = subs

    def unregister(self, channel: str, subscriber: _Subscriber) -> None:
        with self._lock:
            subs = list(self._subscribers.get(channel, ()))
            if subscriber in subs:
                subs.remove(subscriber)
            if subs:
                self._subscribers[channel] = subs
            elif channel in self._subscribers:
                del self._subscribers[channel]

    def publish(self, channel: str, message: Any) -> None:
        with self._lock:
            subscribers = list(self._subscribers.get(channel, ()))
        for subscriber in subscribers:
            if subscriber.closed:
                continue
            subscriber.queue.append({"type": "message", "data": message})


class _PubSub:
    def __init__(self, server: FakeServer, channel: str | None = None) -> None:
        self._server = server
        self._channel = channel
        self._queue: Deque[Any] = deque()
        self._subscriber: _Subscriber | None = None
        self._closed = False

    def subscribe(self, channel: str) -> None:
        if self._closed:
            raise RuntimeError("PubSub already closed")
        self._channel = channel
        self._subscriber = _Subscriber(channel=channel, queue=self._queue)
        self._server.register(channel, self._subscriber)

    def get_message(self, timeout: float | None = None) -> Any:
        if self._closed:
            return None
        if timeout is None:
            timeout = 0.0
        deadline = time.time() + timeout
        while True:
            if self._queue:
                return self._queue.popleft()
            if timeout and time.time() >= deadline:
                return None
            if timeout == 0:
                return None
            time.sleep(min(0.01, max(0.0, deadline - time.time())))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._subscriber is not None:
            self._subscriber.closed = True
            self._server.unregister(self._subscriber.channel, self._subscriber)
        self._queue.clear()


class FakeRedis:
    def __init__(self, server: FakeServer, decode_responses: bool = True) -> None:
        self._server = server
        self._decode = decode_responses

    def pubsub(self, ignore_subscribe_messages: bool = True) -> _PubSub:  # noqa: ARG002 - parity with fakeredis
        return _PubSub(self._server)

    def publish(self, channel: str, message: Any) -> None:
        if self._decode and isinstance(message, bytes):
            message = message.decode("utf-8", errors="ignore")
        self._server.publish(channel, message)

    def close(self) -> None:
        # No resources to release in the stub implementation
        pass


class fakeredis_namespace:
    FakeServer = FakeServer
    FakeRedis = FakeRedis


fakeredis = fakeredis_namespace()

__all__ = ["fakeredis", "FakeServer", "FakeRedis"]
