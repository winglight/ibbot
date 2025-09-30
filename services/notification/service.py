"""Notification service primitives for dispatching events to external channels."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Mapping, MutableMapping, Sequence

from src.ibbot.influx import InfluxPoint, write_points
from src.ibbot.redis import StreamMessage

logger = logging.getLogger("ibbot.services.notification")


@dataclass(slots=True)
class NotificationEvent:
    """Structured representation of an event consumed from Redis streams."""

    stream: str
    message_id: str
    payload: Mapping[str, Any]


@dataclass(slots=True)
class NotificationChannel:
    """Represents an outbound notification channel."""

    name: str
    sender: Callable[[NotificationEvent, str], Awaitable[None] | None]

    async def send(self, event: NotificationEvent, message: str) -> None:
        result = self.sender(event, message)
        if inspect.isawaitable(result):
            await result


@dataclass(slots=True)
class RetryItem:
    """Keep track of retry attempts for a given event/channel combination."""

    event: NotificationEvent
    message: str
    attempts: int = 0
    last_error: str | None = None


class RedisStreamReader:
    """Fetch ``StreamMessage`` entries from multiple Redis streams."""

    def __init__(
        self,
        redis_client: Any,
        streams: Sequence[str],
        *,
        block_ms: int = 1000,
        count: int | None = None,
    ) -> None:
        self._redis = redis_client
        self._streams = list(streams)
        self._block_ms = block_ms
        self._count = count

    async def read(self, last_ids: Mapping[str, str]) -> list[StreamMessage]:
        stream_offsets = {stream: last_ids.get(stream, "0-0") for stream in self._streams}
        response = await self._redis.xread(stream_offsets, block=self._block_ms, count=self._count)
        messages: list[StreamMessage] = []
        for stream, entries in response:
            for message_id, data in entries:
                messages.append(StreamMessage(stream=stream, message_id=message_id, data=data))
        return messages


class NotificationService:
    """Consume events from Redis streams and dispatch notifications."""

    def __init__(
        self,
        *,
        stream_reader: RedisStreamReader | Any,
        channels: Sequence[NotificationChannel],
        poll_interval: float = 1.0,
        max_retries: int = 3,
        write: Callable[[Iterable[InfluxPoint | Mapping[str, Any]]], Awaitable[int] | int] = write_points,
        service_name: str = "notification",
        logger_instance: logging.Logger | None = None,
    ) -> None:
        self._stream_reader = stream_reader
        self._channels = OrderedDict((channel.name, channel) for channel in channels)
        self._poll_interval = max(poll_interval, 0.0)
        self._max_retries = max(0, int(max_retries))
        self._write = write
        self._service_name = service_name
        self._logger = logger_instance or logger
        self._offsets: MutableMapping[str, str] = {}
        self._pending_retries: dict[tuple[str, str, str], RetryItem] = {}
        self._stop_requested = False
        self._last_error: str | None = None

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def stop(self) -> None:
        """Signal the background consumer loop to stop."""

        self._stop_requested = True

    async def run_forever(self) -> None:
        """Continuously poll Redis streams and process events."""

        self._logger.info(
            "Starting notification service", channels=list(self._channels), poll_interval=self._poll_interval
        )
        try:
            while not self._stop_requested:
                processed = await self.poll_once()
                if processed == 0 and self._poll_interval > 0:
                    await asyncio.sleep(self._poll_interval)
        finally:
            self._logger.info("Notification service stopped")

    async def poll_once(self) -> int:
        """Process pending retries and fetch new events once."""

        processed = 0
        processed += await self._process_pending_retries()

        try:
            raw_messages = await self._stream_reader.read(self._offsets)
        except Exception as exc:  # pragma: no cover - network errors hard to simulate deterministically
            self._logger.warning("Failed to read notification streams", exc_info=True)
            self._last_error = str(exc)
            await self._write_log_point(
                stream="internal",
                channel="reader",
                status="error",
                message=f"Failed to read streams: {exc}",
                retries=0,
            )
            return processed

        events = [NotificationEvent(stream=msg.stream, message_id=msg.message_id, payload=msg.data) for msg in raw_messages]
        if not events:
            return processed

        for event in events:
            await self._dispatch_event(event)
            self._offsets[event.stream] = event.message_id
            processed += 1

        return processed

    @property
    def health_status(self) -> Mapping[str, Any]:
        pending = {
            f"{stream}:{message_id}:{channel}": item.attempts
            for (stream, message_id, channel), item in self._pending_retries.items()
        }
        healthy = not pending and self._last_error is None
        return {
            "service": self._service_name,
            "healthy": healthy,
            "last_error": self._last_error,
            "pending_retries": pending,
            "channels": list(self._channels),
        }

    def get_retry_count(self, stream: str, message_id: str, channel: str) -> int:
        item = self._pending_retries.get((stream, message_id, channel))
        return item.attempts if item else 0

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------
    async def _process_pending_retries(self) -> int:
        if not self._pending_retries:
            return 0

        processed = 0
        pending_items = list(self._pending_retries.items())
        for key, item in pending_items:
            channel_name = key[2]
            channel = self._channels.get(channel_name)
            if channel is None:
                self._pending_retries.pop(key, None)
                continue
            try:
                await channel.send(item.event, item.message)
            except Exception as exc:  # pragma: no cover - depends on channel implementation
                item.attempts += 1
                item.last_error = str(exc)
                self._last_error = str(exc)
                await self._write_log_point(
                    stream=item.event.stream,
                    channel=channel_name,
                    status="failed",
                    message=f"Retry failed: {exc}",
                    retries=item.attempts,
                )
                if self._max_retries and item.attempts >= self._max_retries:
                    self._logger.warning(
                        "Dropping notification after exhausting retries",
                        extra={"stream": item.event.stream, "message_id": item.event.message_id, "channel": channel_name},
                    )
                    self._pending_retries.pop(key, None)
                continue

            await self._write_log_point(
                stream=item.event.stream,
                channel=channel_name,
                status="success",
                message=item.message,
                retries=item.attempts,
            )
            self._pending_retries.pop(key, None)
            processed += 1

        if processed and not self._pending_retries:
            self._last_error = None

        return processed

    async def _dispatch_event(self, event: NotificationEvent) -> None:
        if not self._channels:
            self._logger.debug("No notification channels configured; skipping event", extra={"stream": event.stream})
            return

        message = self._format_event(event)
        for channel in self._channels.values():
            try:
                await channel.send(event, message)
            except Exception as exc:
                self._logger.warning(
                    "Failed to send notification",
                    extra={"channel": channel.name, "stream": event.stream, "message_id": event.message_id},
                    exc_info=True,
                )
                attempts = self._schedule_retry(event, channel.name, message, str(exc))
                await self._write_log_point(
                    stream=event.stream,
                    channel=channel.name,
                    status="failed",
                    message=f"{message} | error={exc}",
                    retries=attempts,
                )
                self._last_error = str(exc)
                continue

            await self._write_log_point(
                stream=event.stream,
                channel=channel.name,
                status="success",
                message=message,
                retries=self.get_retry_count(event.stream, event.message_id, channel.name),
            )
            if (event.stream, event.message_id, channel.name) not in self._pending_retries:
                self._last_error = None

    def _schedule_retry(self, event: NotificationEvent, channel: str, message: str, error: str) -> int:
        key = (event.stream, event.message_id, channel)
        item = self._pending_retries.get(key)
        if item is None:
            item = RetryItem(event=event, message=message, attempts=1, last_error=error)
            self._pending_retries[key] = item
        else:
            item.attempts += 1
            item.last_error = error
        return item.attempts

    def _format_event(self, event: NotificationEvent) -> str:
        try:
            payload = {"stream": event.stream, "message_id": event.message_id, "payload": event.payload}
            return json.dumps(payload, ensure_ascii=False, default=self._safe_repr)
        except Exception:
            return f"{event.stream}:{event.message_id}:{event.payload}"

    @staticmethod
    def _safe_repr(value: Any) -> str:
        return repr(value)

    async def _write_log_point(
        self,
        *,
        stream: str,
        channel: str,
        status: str,
        message: str,
        retries: int,
    ) -> None:
        point = InfluxPoint(
            "logs",
            fields={
                "message": message,
                "status": status,
                "retries": retries,
            },
            tags={
                "service": self._service_name,
                "stream": stream,
                "channel": channel,
                "status": status,
            },
            timestamp=datetime.now(timezone.utc),
        )
        try:
            result = self._write([point])
            if inspect.isawaitable(result):
                await result
        except Exception:  # pragma: no cover - logging only, depends on influx availability
            self._logger.debug("Failed to write notification log point", exc_info=True)
