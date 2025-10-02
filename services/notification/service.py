"""Notification service primitives for dispatching events to external channels."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Mapping, MutableMapping, Sequence

from src.ibbot.influx import InfluxPoint, write_points
from src.ibbot import update_service_health
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
        redis_client: Any | None = None,
        command_queue: str = "queue:notification:dispatch",
        command_status_prefix: str = "hash:notification:command",
        command_batch_size: int = 10,
        history_key: str = "list:notification:history",
        history_max_entries: int = 500,
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
        self._redis = redis_client
        self._command_queue = command_queue
        self._command_status_prefix = command_status_prefix.rstrip(":")
        self._command_batch_size = max(1, int(command_batch_size))
        self._history_key = history_key
        self._history_max_entries = max(0, int(history_max_entries))

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
                await self._publish_health_metrics()
                if processed == 0 and self._poll_interval > 0:
                    await asyncio.sleep(self._poll_interval)
        finally:
            self._logger.info("Notification service stopped")

    async def poll_once(self, *, include_commands: bool = True) -> int:
        """Process queued commands, pending retries, and fetch new events once."""

        processed = 0
        if include_commands:
            processed += await self._process_command_queue()
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
    async def _process_command_queue(self) -> int:
        if self._redis is None or not self._command_queue:
            return 0

        processed = 0
        for _ in range(self._command_batch_size):
            try:
                raw = await self._redis.lpop(self._command_queue)
            except Exception as exc:
                self._logger.warning("Failed to read notification command queue", exc_info=True)
                self._last_error = str(exc)
                break
            if raw is None:
                break
            processed += 1
            await self._handle_command(raw)
        return processed

    async def _handle_command(self, raw: Any) -> None:
        try:
            data = json.loads(raw)
        except Exception:
            self._logger.warning("Discarding malformed notification command", extra={"payload": raw})
            return

        command_id = str(data.get("command_id") or uuid.uuid4().hex)
        command_type = str(data.get("type") or "dispatch")
        payload = data.get("payload")
        await self._update_command_status(
            command_id,
            status="processing",
            type=command_type,
        )

        results: dict[str, Any] | None = None
        error: str | None = None
        try:
            results = await self._execute_command(command_id, command_type, payload)
        except Exception as exc:
            error = str(exc)
            self._last_error = error
            self._logger.warning(
                "Notification command failed",
                extra={"command_id": command_id, "type": command_type},
                exc_info=True,
            )

        status = "failed"
        if results is not None:
            statuses = [entry.get("status") for entry in results.get("results", {}).values()]
            if statuses and all(value == "success" for value in statuses):
                status = "success"
                self._last_error = None
            elif any(value == "success" for value in statuses):
                status = "partial"
                if error is None:
                    error = self._last_error
            elif error is None:
                error = self._last_error

        mapping: dict[str, Any] = {"status": status}
        if results is not None:
            mapping["results"] = json.dumps(results.get("results", {}), ensure_ascii=False)
            mapping["event"] = json.dumps(results.get("event", {}), ensure_ascii=False)
        if error:
            mapping["last_error"] = error
        await self._update_command_status(command_id, **mapping)

    async def _execute_command(
        self, command_id: str, command_type: str, payload: Any
    ) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ValueError("Command payload must be a mapping")

        raw_channels: Iterable[str] | None
        if "channels" in payload and isinstance(payload["channels"], Iterable):
            raw_channels = payload["channels"]  # type: ignore[assignment]
        elif payload.get("channel"):
            raw_channels = [payload["channel"]]
        else:
            raw_channels = None

        allowed_channels: set[str] | None = None
        if raw_channels:
            allowed_channels = {str(value).strip().lower() for value in raw_channels if str(value).strip()}
        message_id = command_id
        stream = str(payload.get("stream") or f"command:{command_type}")
        event_payload = dict(payload)
        event_payload.setdefault("command_id", command_id)
        event = NotificationEvent(stream=stream, message_id=message_id, payload=event_payload)

        results: dict[str, Any] = {}
        await self._dispatch_event(event, allowed_channels=allowed_channels, results=results)

        if allowed_channels is not None:
            missing = [name for name in allowed_channels if name not in self._channels]
            for name in missing:
                results.setdefault(name, {"status": "missing", "error": "Channel not configured"})

        return {"results": results, "event": {"stream": event.stream, "message_id": event.message_id}}

    async def _update_command_status(self, command_id: str, **fields: Any) -> None:
        if self._redis is None:
            return
        mapping: dict[str, str] = {}
        for key, value in fields.items():
            if value is None:
                continue
            if isinstance(value, (str, bytes)):
                serialised = value.decode() if isinstance(value, bytes) else value
            else:
                serialised = str(value)
            mapping[str(key)] = serialised
        mapping["updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            await self._redis.hset(self._command_status_key(command_id), mapping=mapping)
        except Exception as exc:
            self._logger.debug("Failed to persist command status", exc_info=True)
            self._last_error = str(exc)

    def _command_status_key(self, command_id: str) -> str:
        return f"{self._command_status_prefix}:{command_id}"

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
                await self._record_history(
                    event=item.event,
                    channel=channel_name,
                    status="failed",
                    message=item.message,
                    retries=item.attempts,
                    error=str(exc),
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
            await self._record_history(
                event=item.event,
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

    async def _dispatch_event(
        self,
        event: NotificationEvent,
        *,
        allowed_channels: Iterable[str] | None = None,
        results: MutableMapping[str, Any] | None = None,
    ) -> None:
        if not self._channels:
            self._logger.debug("No notification channels configured; skipping event", extra={"stream": event.stream})
            return

        allowed_set = None
        if allowed_channels is not None:
            allowed_set = {str(name).strip().lower() for name in allowed_channels if str(name).strip()}

        message = self._format_event(event)
        for channel in self._channels.values():
            if allowed_set is not None and channel.name not in allowed_set:
                continue
            try:
                await channel.send(event, message)
            except Exception as exc:
                self._logger.warning(
                    "Failed to send notification",
                    extra={"channel": channel.name, "stream": event.stream, "message_id": event.message_id},
                    exc_info=True,
                )
                attempts = self._schedule_retry(event, channel.name, message, str(exc))
                if results is not None:
                    results[channel.name] = {"status": "retrying", "error": str(exc), "retries": attempts}
                await self._write_log_point(
                    stream=event.stream,
                    channel=channel.name,
                    status="failed",
                    message=f"{message} | error={exc}",
                    retries=attempts,
                )
                await self._record_history(
                    event=event,
                    channel=channel.name,
                    status="failed",
                    message=message,
                    retries=attempts,
                    error=str(exc),
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
            await self._record_history(
                event=event,
                channel=channel.name,
                status="success",
                message=message,
                retries=self.get_retry_count(event.stream, event.message_id, channel.name),
            )
            if results is not None:
                results[channel.name] = {
                    "status": "success",
                    "retries": self.get_retry_count(event.stream, event.message_id, channel.name),
                }
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

    async def _record_history(
        self,
        *,
        event: NotificationEvent,
        channel: str,
        status: str,
        message: str,
        retries: int,
        error: str | None = None,
    ) -> None:
        if self._redis is None or not self._history_key:
            return

        entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "stream": event.stream,
            "message_id": event.message_id,
            "channel": channel,
            "status": status,
            "message": message,
            "retries": retries,
        }
        payload = event.payload
        if isinstance(payload, Mapping):
            for field in ("event_type", "category", "command_id"):
                if field in payload:
                    entry[field] = payload[field]
        if error:
            entry["error"] = error

        try:
            serialised = json.dumps(entry, ensure_ascii=False, default=self._safe_repr)
        except TypeError:
            entry.pop("message", None)
            serialised = json.dumps(entry, ensure_ascii=False, default=self._safe_repr)

        try:
            await self._redis.lpush(self._history_key, serialised)
            if self._history_max_entries:
                await self._redis.ltrim(self._history_key, 0, self._history_max_entries - 1)
        except Exception:  # pragma: no cover - storage errors shouldn't crash processing
            self._logger.debug("Failed to record notification history", exc_info=True)

    async def _publish_health_metrics(self) -> None:
        if self._redis is None:
            return
        queue_pending = 0
        try:
            queue_pending = int(await self._redis.llen(self._command_queue))
        except Exception:
            self._logger.debug("Failed to read notification queue length", exc_info=True)
        pending_total = queue_pending + len(self._pending_retries)
        metrics = {"pending": pending_total, "last_error": self._last_error or ""}
        try:
            await update_service_health(
                self._redis,
                self._service_name,
                healthy=self._last_error is None,
                last_error=self._last_error,
                metrics=metrics,
            )
        except Exception:
            self._logger.debug("Failed to update notification health", exc_info=True)

    @property
    def redis(self) -> Any | None:
        return self._redis

    @property
    def command_queue(self) -> str:
        return self._command_queue

    @property
    def command_status_prefix(self) -> str:
        return self._command_status_prefix

    @property
    def history_key(self) -> str:
        return self._history_key

    @property
    def history_max_entries(self) -> int:
        return self._history_max_entries
