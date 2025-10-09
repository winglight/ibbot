"""Utility classes for Redis-driven strategy subscriptions."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Iterable, Mapping

import orjson
from loguru import logger


@dataclass(slots=True)
class _ChannelState:
    """Track subscription state for a single Redis pub/sub channel."""

    queue: asyncio.Queue[Mapping[str, Any]]
    ready: asyncio.Event
    latest: Mapping[str, Any] | None = None
    last_message_at: float = 0.0


class StrategyEventStream:
    """Bridge Redis pub/sub channels into asyncio friendly queues."""

    def __init__(
        self,
        redis_client: Any,
        *,
        channels: Iterable[str],
        heartbeat_interval: float = 5.0,
        heartbeat_timeout: float = 15.0,
        queue_size: int = 256,
    ) -> None:
        self._redis = redis_client
        self._channels = tuple(dict.fromkeys(str(channel) for channel in channels))
        self._heartbeat_interval = max(0.5, float(heartbeat_interval))
        self._heartbeat_timeout = max(self._heartbeat_interval, float(heartbeat_timeout))
        self._queue_size = max(1, int(queue_size))

        self._channel_state: dict[str, _ChannelState] = {
            channel: _ChannelState(
                queue=asyncio.Queue(maxsize=self._queue_size),
                ready=asyncio.Event(),
            )
            for channel in self._channels
        }

        self._pubsub: Any | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._closed = asyncio.Event()
        self._logger = logger.bind(module="strategy.event_stream")

    async def __aenter__(self) -> "StrategyEventStream":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        await self.close()

    async def start(self) -> None:
        """Start the Redis pub/sub consumer tasks."""

        if self._reader_task is not None:
            return
        await self._subscribe()
        self._reader_task = asyncio.create_task(self._reader_loop(), name="strategy-event-reader")
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name="strategy-event-heartbeat"
        )

    async def close(self) -> None:
        """Stop background tasks and release Redis resources."""

        if self._closed.is_set():
            return
        self._closed.set()

        tasks = [task for task in (self._reader_task, self._heartbeat_task) if task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

        self._reader_task = None
        self._heartbeat_task = None

        pubsub = self._pubsub
        self._pubsub = None
        if pubsub is None:
            return
        close = getattr(pubsub, "close", None)
        if close is None:
            return
        if asyncio.iscoroutinefunction(close):
            await close()  # pragma: no branch - depends on redis implementation
        else:  # pragma: no cover - synchronous close path
            close()

    async def wait_until_ready(self, *, timeout: float | None = None) -> None:
        """Wait until all channels have received at least one event."""

        if not self._channel_state:
            return
        tasks = [state.ready.wait() for state in self._channel_state.values()]
        try:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=timeout)
        except asyncio.TimeoutError as exc:
            missing = [channel for channel, state in self._channel_state.items() if not state.ready.is_set()]
            raise RuntimeError(
                f"strategy market data channels not ready: {', '.join(missing)}"
            ) from exc

    async def next_event(
        self, channel: str, *, timeout: float | None = None
    ) -> Mapping[str, Any]:
        """Return the next event for *channel*."""

        state = self._channel_state.get(channel)
        if state is None:
            raise KeyError(f"channel '{channel}' is not subscribed")
        if timeout is None:
            payload = await state.queue.get()
        else:
            payload = await asyncio.wait_for(state.queue.get(), timeout=timeout)
        return payload

    def iter_channel(self, channel: str) -> AsyncIterator[Mapping[str, Any]]:
        """Return an async iterator yielding events for *channel*."""

        async def _iterator() -> AsyncIterator[Mapping[str, Any]]:
            while True:
                yield await self.next_event(channel)

        return _iterator()

    def latest_event(self, channel: str) -> Mapping[str, Any] | None:
        """Return the latest observed payload for *channel*, if available."""

        state = self._channel_state.get(channel)
        return state.latest if state else None

    async def _subscribe(self) -> None:
        pubsub = getattr(self._redis, "pubsub", None)
        if pubsub is None or not callable(pubsub):
            raise RuntimeError("Redis client does not support pubsub()")
        self._pubsub = pubsub()
        try:
            await self._pubsub.subscribe(*self._channels)
        except Exception:
            await self._pubsub.close()
            self._pubsub = None
            raise

    async def _reset_pubsub(self) -> None:
        pubsub = self._pubsub
        self._pubsub = None
        if pubsub is not None:
            try:
                await pubsub.close()
            except Exception:  # pragma: no cover - defensive guard
                pass
        await self._subscribe()

    async def _reader_loop(self) -> None:
        """Background task that forwards Redis pub/sub messages."""

        while not self._closed.is_set():
            try:
                pubsub = self._pubsub
                if pubsub is None:
                    await self._reset_pubsub()
                    pubsub = self._pubsub
                    if pubsub is None:
                        await asyncio.sleep(0.1)
                        continue
                async for message in pubsub.listen():
                    if self._closed.is_set():
                        return
                    if not isinstance(message, Mapping):
                        continue
                    if message.get("type") != "message":
                        continue
                    channel_raw = message.get("channel")
                    data_raw = message.get("data")
                    if isinstance(channel_raw, (bytes, bytearray)):
                        channel = channel_raw.decode("utf-8", errors="ignore")
                    else:
                        channel = str(channel_raw)
                    state = self._channel_state.get(channel)
                    if state is None:
                        continue
                    payload = self._decode_message(data_raw)
                    state.latest = payload
                    state.last_message_at = time.monotonic()
                    if state.queue.full():
                        try:
                            state.queue.get_nowait()
                        except asyncio.QueueEmpty:  # pragma: no cover - race guard
                            pass
                    state.queue.put_nowait(payload)
                    state.ready.set()
            except asyncio.CancelledError:  # pragma: no cover - cancellation path
                raise
            except Exception as exc:  # pragma: no cover - reconnect path
                self._logger.warning("Redis pub/sub listener error: %s", exc)
                await asyncio.sleep(0.5)
                try:
                    await self._reset_pubsub()
                except Exception as reset_exc:  # pragma: no cover - defensive guard
                    self._logger.warning("Failed to reset pub/sub connection: %s", reset_exc)
                    await asyncio.sleep(1.0)

    async def _heartbeat_loop(self) -> None:
        """Ping Redis periodically and refresh subscriptions when stale."""

        while not self._closed.is_set():
            try:
                await asyncio.wait_for(self._closed.wait(), timeout=self._heartbeat_interval)
                break
            except asyncio.TimeoutError:
                pass
            now = time.monotonic()
            stale = False
            for state in self._channel_state.values():
                if not state.ready.is_set():
                    continue
                if now - state.last_message_at > self._heartbeat_timeout:
                    stale = True
                    break
            try:
                ping = getattr(self._redis, "ping", None)
                if ping is not None:
                    if asyncio.iscoroutinefunction(ping):
                        await ping()
                    else:  # pragma: no cover - synchronous ping
                        ping()
            except Exception as exc:  # pragma: no cover - heartbeat failure path
                self._logger.warning("Redis heartbeat failed: %s", exc)
                stale = True
            if stale:
                self._logger.info("Refreshing Redis pub/sub subscriptions due to inactivity")
                try:
                    await self._reset_pubsub()
                except Exception as exc:  # pragma: no cover - defensive guard
                    self._logger.warning("Failed to refresh subscriptions: %s", exc)

    @staticmethod
    def _decode_message(raw: Any) -> Mapping[str, Any]:
        if isinstance(raw, (bytes, bytearray)):
            try:
                return orjson.loads(raw)
            except orjson.JSONDecodeError:
                text = raw.decode("utf-8", errors="ignore")
                return {"value": text}
        if isinstance(raw, str):
            try:
                return orjson.loads(raw)
            except orjson.JSONDecodeError:
                return {"value": raw}
        if isinstance(raw, Mapping):
            return raw
        return {"value": raw}

