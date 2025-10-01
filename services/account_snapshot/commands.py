"""Redis command consumer for account snapshot refresh operations."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional, Protocol

from loguru import logger

from ibbot.redis import RedisSettings, close_redis, create_redis
from src.account_snapshot_cache import (
    write_account_info_snapshot,
    write_positions_snapshot,
)
from src.utils import now_local

try:  # pragma: no cover - redis may be optional in certain environments
    from redis.asyncio import Redis
except Exception:  # pragma: no cover
    Redis = None  # type: ignore


class AccountDataProvider(Protocol):
    """Protocol describing the data provider used by the command consumer."""

    async def get_account_info(self, *, reason: str) -> Dict[str, Any]:
        ...

    async def get_positions(self, *, fast: bool, reason: str) -> Any:
        ...


class IBAccountDataProvider:
    """Resolve account information via the shared ``IBKRClient`` instance."""

    def __init__(self) -> None:
        self._client: Any = None

    async def _resolve_client(self) -> Any:
        if self._client is not None:
            return self._client

        try:
            import core
        except ImportError as exc:  # pragma: no cover - defensive guard
            raise RuntimeError("core module unavailable for IB account provider") from exc

        client = core.get_ibkr_client()
        if client is None:
            factory = getattr(core, "IBKRClient", None)
            if callable(factory):
                client = factory()
                try:
                    core.set_ibkr_client(client)
                except Exception:  # pragma: no cover - best effort cache
                    pass

        if client is None:
            raise RuntimeError("IBKR client unavailable for account snapshots")

        self._client = client
        return client

    async def get_account_info(self, *, reason: str) -> Dict[str, Any]:
        client = await self._resolve_client()
        return await client.get_account_info()

    async def get_positions(self, *, fast: bool, reason: str) -> Any:
        client = await self._resolve_client()
        return await client.get_positions(fast=fast)


@dataclass(slots=True)
class CommandResult:
    status: str
    data: Any | None
    error: Optional[str] = None


class AccountCommandConsumer:
    """Consume account related commands and persist the resulting snapshots."""

    SUPPORTED_COMMANDS = {"get_account_info", "get_positions"}

    def __init__(
        self,
        *,
        config: "AccountSnapshotServiceConfig",
        provider: Optional[AccountDataProvider] = None,
        redis_settings: Optional[RedisSettings] = None,
        redis_factory: Optional[Callable[[], Awaitable[Optional[Redis]]]] = None,
        retry_attempts: Optional[int] = None,
        retry_delay_seconds: float = 0.2,
    ) -> None:
        from ibbot.config import AccountSnapshotServiceConfig

        if not isinstance(config, AccountSnapshotServiceConfig):  # pragma: no cover - defensive
            raise TypeError("config must be an AccountSnapshotServiceConfig instance")

        self.config = config
        self._provider = provider or IBAccountDataProvider()
        self._retry_attempts = max(1, retry_attempts or config.command_retry_attempts)
        self._retry_delay = max(0.0, retry_delay_seconds)
        self._redis_settings = redis_settings or RedisSettings(url=config.redis_url)
        self._redis_factory = redis_factory or self._create_redis

        self._redis: Optional[Redis] = None
        self._task: Optional[asyncio.Task[Any]] = None
        self._stop_event = asyncio.Event()
        self._logger = logger.bind(module="account_snapshot_consumer", request_id="-")

    async def _create_redis(self) -> Optional[Redis]:
        return await create_redis(settings=self._redis_settings)

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("AccountCommandConsumer already started")

        redis = await self._redis_factory()
        if redis is None:
            raise RuntimeError("Redis connection unavailable for AccountCommandConsumer")

        self._redis = redis
        self._stop_event.clear()
        self._task = asyncio.create_task(self._consume_loop())
        self._logger.info("Account command consumer started")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return

        self._stop_event.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        self._task = None

        if self._redis is not None:
            await close_redis(self._redis)
            self._redis = None

        self._logger.info("Account command consumer stopped")

    async def _consume_loop(self) -> None:
        assert self._redis is not None
        redis = self._redis
        queue = self.config.command_queue_key

        while not self._stop_event.is_set():
            try:
                entry = await redis.blpop(queue, timeout=1)
            except asyncio.CancelledError:  # pragma: no cover - cooperative shutdown
                raise
            except Exception as exc:
                self._logger.warning("Failed to read command queue", error=str(exc))
                await asyncio.sleep(1.0)
                continue

            if entry is None:
                await self._update_health(status="idle")
                continue

            _, raw = entry
            try:
                message = json.loads(raw)
            except Exception:
                self._logger.debug("Discarding malformed command payload", raw=raw)
                continue

            await self._process_message(message, raw)

    async def _process_message(self, message: Dict[str, Any], raw: str) -> None:
        command = message.get("command")
        command_id = message.get("command_id") or ""
        source = message.get("source")

        if command not in self.SUPPORTED_COMMANDS:
            if source and source != self.config.service_name:
                await self._requeue(raw)
                return
            payload = message.get("payload") or {}
            reason = str(payload.get("reason", "unspecified"))
            await self._handle_failure(
                command or "unknown",
                command_id,
                reason,
                RuntimeError(f"Unsupported command: {command}"),
            )
            return

        payload = message.get("payload") or {}
        reason = str(payload.get("reason", "unspecified"))
        fast = bool(payload.get("fast", True))

        try:
            result = await self._execute_with_retry(command, reason=reason, fast=fast)
        except Exception as exc:
            await self._handle_failure(command, command_id, reason, exc)
            return

        await self._handle_success(command, command_id, reason, result)

    async def _requeue(self, raw: str) -> None:
        redis = self._redis
        if redis is None:
            return
        try:
            await redis.rpush(self.config.command_queue_key, raw)
        except Exception:
            self._logger.debug("Failed to requeue unsupported command")
        await asyncio.sleep(0.1)

    async def _execute_with_retry(
        self,
        command: str,
        *,
        reason: str,
        fast: bool,
    ) -> Any:
        last_exc: Optional[Exception] = None

        for attempt in range(1, self._retry_attempts + 1):
            try:
                if command == "get_account_info":
                    coro = self._provider.get_account_info(reason=reason)
                elif command == "get_positions":
                    coro = self._provider.get_positions(reason=reason, fast=fast)
                else:  # pragma: no cover - guarded earlier
                    raise RuntimeError(f"Unsupported command: {command}")

                return await asyncio.wait_for(
                    coro,
                    timeout=float(self.config.command_timeout_seconds),
                )
            except asyncio.TimeoutError as exc:
                last_exc = exc
                if attempt < self._retry_attempts:
                    self._logger.warning(
                        "Account command timed out; retrying",
                        command=command,
                        attempt=attempt,
                        retries=self._retry_attempts,
                    )
                    await asyncio.sleep(self._retry_delay)
                else:
                    self._logger.warning(
                        "Account command timed out after retries",
                        command=command,
                        retries=self._retry_attempts,
                    )
            except Exception as exc:
                last_exc = exc
                break

        assert last_exc is not None
        raise last_exc

    async def _handle_success(
        self,
        command: str,
        command_id: str,
        reason: str,
        result: Any,
    ) -> None:
        if command == "get_account_info":
            await write_account_info_snapshot(result, reason)
        elif command == "get_positions":
            await write_positions_snapshot(result, reason)

        await self._publish_stream_event(
            command=command,
            command_id=command_id,
            status="ok",
            reason=reason,
            payload=result,
        )
        await self._write_command_result(
            command_id,
            CommandResult(status="ok", data=result),
        )
        await self._update_health(
            status="ok",
            command=command,
            command_id=command_id,
            error=None,
        )

    async def _handle_failure(
        self,
        command: str,
        command_id: str,
        reason: str,
        exc: Exception,
    ) -> None:
        error_message = str(exc) or exc.__class__.__name__
        await self._publish_stream_event(
            command=command,
            command_id=command_id,
            status="error",
            reason=reason,
            payload={"error": error_message},
        )
        await self._write_command_result(
            command_id,
            CommandResult(status="error", data=None, error=error_message),
        )
        await self._update_health(
            status="error",
            command=command,
            command_id=command_id,
            error=error_message,
        )

    async def _publish_stream_event(
        self,
        *,
        command: str,
        command_id: str,
        status: str,
        reason: str,
        payload: Any,
    ) -> None:
        redis = self._redis
        if redis is None:
            return

        event = {
            "command": command,
            "command_id": command_id,
            "status": status,
            "reason": reason,
            "source": self.config.service_name,
            "timestamp": now_local().isoformat(),
        }
        if payload is not None:
            try:
                event["payload"] = json.dumps(payload, ensure_ascii=False)
            except Exception:
                event["payload"] = json.dumps({"repr": repr(payload)}, ensure_ascii=False)

        try:
            await redis.xadd(
                self.config.snapshot_stream_key,
                event,
                maxlen=self.config.snapshot_stream_maxlen,
                approximate=True,
            )
        except Exception as exc:
            self._logger.warning("Failed to publish snapshot stream event", error=str(exc))

    async def _write_command_result(self, command_id: str, result: CommandResult) -> None:
        redis = self._redis
        if redis is None or not command_id:
            return

        key = f"{self.config.command_result_prefix}{command_id}"
        payload = {
            "status": result.status,
            "service": self.config.service_name,
        }
        if result.data is not None:
            payload["data"] = result.data
        if result.error:
            payload["error"] = result.error

        try:
            await redis.set(key, json.dumps(payload, ensure_ascii=False))
            ttl = int(self.config.command_result_ttl_seconds)
            if ttl > 0:
                await redis.expire(key, ttl)
        except Exception as exc:
            self._logger.warning(
                "Failed to persist command result", command_id=command_id, error=str(exc)
            )

    async def _update_health(
        self,
        *,
        status: str,
        command: Optional[str] = None,
        command_id: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        redis = self._redis
        if redis is None:
            return

        timestamp = now_local().isoformat()
        mapping: Dict[str, Any] = {
            "service": self.config.service_name,
            "status": status,
            "last_seen": timestamp,
            "updated_at": timestamp,
        }

        if command:
            mapping["last_command"] = command
        if command_id:
            mapping["last_command_id"] = command_id

        if error:
            mapping["last_error"] = error
            mapping["last_error_at"] = timestamp
        else:
            mapping["last_error"] = ""
            if status == "ok":
                mapping["last_success"] = timestamp
        if error:
            mapping["last_failure"] = timestamp

        try:
            queue_depth = await redis.llen(self.config.command_queue_key)
        except Exception:
            queue_depth = None
        if queue_depth is not None:
            mapping["queue_depth"] = str(queue_depth)

        try:
            await redis.hset(self.config.health_hash_key, mapping=mapping)
        except Exception as exc:
            self._logger.warning("Failed to update service health", error=str(exc))


__all__ = [
    "AccountCommandConsumer",
    "AccountDataProvider",
    "CommandResult",
    "IBAccountDataProvider",
]
