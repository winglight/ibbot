"""Core components for the IB command service."""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Mapping, MutableMapping

from src.ibbot.logging import get_logger
from src.ibbot.redis import (
    DEFAULT_COMMAND_QUEUE,
    get_queue_length,
)
from src.utils.ibkr_serialization import (
    coerce_ibkr_value,
    fill_to_dict,
    is_fill_like,
    is_trade_like,
    trade_to_dict,
)

try:  # redis is optional in some test environments
    from redis.asyncio import Redis
    from redis.exceptions import RedisError
except Exception:  # pragma: no cover - fallback when redis is unavailable
    Redis = Any  # type: ignore[misc, assignment]

    class RedisError(Exception):
        """Fallback RedisError used when redis is unavailable."""



class IBCommandError(Exception):
    """Base exception for command execution errors."""

    def __init__(self, message: str, *, error_code: int | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code


class RetryableIBCommandError(IBCommandError):
    """Exception raised when a command should be retried."""


class IBCommandExecutionError(IBCommandError):
    """Exception raised for fatal command execution errors."""


@dataclass(slots=True)
class ExecutionResult:
    """Structured result returned by the executor."""

    command_id: str
    status: str
    result: Any | None
    attempts: int
    error: str | None = None
    error_code: int | None = None


class IBCommandExecutor:
    """Invoke methods on the injected IB client."""

    def __init__(self, ib_client: Any, *, logger_name: str = "ibbot.services.ib_command.executor") -> None:
        self._ib_client = ib_client
        self._logger = get_logger(logger_name)

    def describe_connectivity(self) -> tuple[bool | None, str | None]:
        """Return the IB client's connectivity flag and last connection error."""

        client = self._ib_client
        if client is None:
            return None, "ib client unavailable"

        connected: bool | None = None
        error: str | None = None

        for attr_name in ("is_connected", "isConnected", "connected"):
            if not hasattr(client, attr_name):
                continue

            attr = getattr(client, attr_name)
            try:
                if callable(attr):
                    result = attr()
                else:
                    result = attr
            except Exception as exc:  # pragma: no cover - defensive guard
                self._logger.debug(
                    "Failed to query IB client connectivity via %s", attr_name, exc_info=exc
                )
                continue

            if result is None:
                continue

            connected = bool(result)
            break

        # Prefer connection_manager if exposed on the client
        connection_manager = getattr(client, "connection_manager", None)
        if connection_manager is None and hasattr(client, "get_connection_manager"):
            try:
                connection_manager = client.get_connection_manager()
            except Exception:  # pragma: no cover - defensive guard
                connection_manager = None

        if connection_manager is not None and hasattr(connection_manager, "get_last_connect_error"):
            try:
                raw_error = connection_manager.get_last_connect_error()
            except Exception:  # pragma: no cover - defensive guard
                raw_error = None
        elif hasattr(client, "get_last_connect_error"):
            try:
                raw_error = client.get_last_connect_error()
            except Exception:  # pragma: no cover - defensive guard
                raw_error = None
        else:
            raw_error = None

        if isinstance(raw_error, str):
            error = raw_error.strip() or None
        elif raw_error:
            error = str(raw_error)

        if connected:
            error = None

        if connected is None and error is not None:
            connected = False

        return connected, error

    async def execute(self, command: Mapping[str, Any], *, attempts: int) -> ExecutionResult:
        command_id = str(command.get("command_id") or "")
        method_name = command.get("method") or command.get("action")
        args = command.get("args") or []
        kwargs = command.get("kwargs") or {}

        if not method_name:
            raise IBCommandExecutionError("command missing 'method' field")

        if not hasattr(self._ib_client, method_name):
            raise IBCommandExecutionError(f"IB client has no method '{method_name}'")

        method = getattr(self._ib_client, method_name)

        ensure_fn = getattr(self._ib_client, "ensure_connection", None)
        if callable(ensure_fn):
            try:
                await ensure_fn(reason=f"ib-command:{method_name}")
            except Exception as exc:
                raise RetryableIBCommandError(str(exc)) from exc

        self._logger.info(
            "Executing IB command",
            extra={
                "command_id": command_id,
                "method": method_name,
                "attempt": attempts,
            },
        )

        try:
            result = await self._invoke(method, *args, **kwargs)
        except RetryableIBCommandError:
            raise
        except IBCommandError:
            raise
        except Exception as exc:  # pragma: no cover - defensive wrap
            raise IBCommandExecutionError(str(exc)) from exc

        return ExecutionResult(
            command_id=command_id,
            status="success",
            result=result,
            attempts=attempts,
        )

    async def _invoke(self, method: Any, *args: Any, **kwargs: Any) -> Any:
        value = method(*args, **kwargs)
        if inspect.isawaitable(value):
            return await value
        return value


class ResultPublisher:
    """Publish command execution results to Redis and log files."""

    def __init__(
        self,
        redis: Redis,
        *,
        stream_key: str = "stream:ib:results",
        hash_prefix: str = "hash:ib:result",
        logger_name: str = "ibbot.services.ib_command.results",
    ) -> None:
        self._redis = redis
        self._stream_key = stream_key
        self._hash_prefix = hash_prefix.rstrip(":")
        self._logger = get_logger(logger_name)

    async def publish(self, payload: ExecutionResult) -> None:
        message = self._to_message(payload)
        await self._redis.xadd(self._stream_key, message)
        await self._redis.hset(self._hash_key(payload.command_id), mapping=message)

        if payload.status == "success":
            self._logger.info(
                "IB command succeeded",
                extra={
                    "command_id": payload.command_id,
                    "attempts": payload.attempts,
                },
            )
        else:
            self._logger.error(
                "IB command failed",
                extra={
                    "command_id": payload.command_id,
                    "attempts": payload.attempts,
                    "error": payload.error,
                },
            )

    def _hash_key(self, command_id: str) -> str:
        suffix = command_id or "unknown"
        return f"{self._hash_prefix}:{suffix}"

    def _to_message(self, payload: ExecutionResult) -> MutableMapping[str, str]:
        message: dict[str, str] = {
            "command_id": payload.command_id,
            "status": payload.status,
            "attempts": str(payload.attempts),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if payload.result is not None:
            message["result"] = self._serialize(payload.result)
        if payload.error:
            message["error"] = payload.error
        if payload.error_code is not None:
            message["error_code"] = str(payload.error_code)
        return message

    def _serialize(self, value: Any) -> str:
        normalised = self._normalise_result_value(value)
        try:
            return json.dumps(normalised, ensure_ascii=False)
        except TypeError:
            safe_value = self._ensure_json_safe(normalised)
            return json.dumps(safe_value, ensure_ascii=False)

    def _normalise_result_value(self, value: Any) -> Any:
        if is_trade_like(value):
            return trade_to_dict(value)
        if is_fill_like(value):
            return fill_to_dict(value)
        if is_dataclass(value):
            return self._normalise_result_value(asdict(value))
        if isinstance(value, SimpleNamespace):
            return {
                key: self._normalise_result_value(val)
                for key, val in vars(value).items()
            }
        if isinstance(value, Mapping):
            return {
                str(key): self._normalise_result_value(val)
                for key, val in value.items()
            }
        if isinstance(value, (list, tuple, set)):
            return [self._normalise_result_value(item) for item in value]
        coerced = coerce_ibkr_value(value)
        if coerced is not value:
            return self._normalise_result_value(coerced)
        if isinstance(value, (bytes, bytearray)):
            try:
                return value.decode("utf-8")
            except Exception:
                return value.hex()
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)

    def _ensure_json_safe(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(key): self._ensure_json_safe(val) for key, val in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [self._ensure_json_safe(item) for item in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)


class CommandConsumer:
    """Consume IB commands from Redis and execute them sequentially."""

    def __init__(
        self,
        redis: Redis,
        executor: IBCommandExecutor,
        publisher: ResultPublisher,
        *,
        queue_key: str = DEFAULT_COMMAND_QUEUE,
        poll_timeout: float = 1.0,
        default_max_retries: int = 3,
        retry_delay: float = 1.0,
        health_key: str = "hash:service:health:ib-command",
        logger_name: str = "ibbot.services.ib_command.consumer",
        publish_retry_attempts: int = 3,
    ) -> None:
        self._redis = redis
        self._executor = executor
        self._publisher = publisher
        self._queue_key = queue_key
        self._poll_timeout = poll_timeout
        self._default_max_retries = max(0, int(default_max_retries))
        self._retry_delay = max(0.0, float(retry_delay))
        self._health_key = health_key
        self._logger = get_logger(logger_name)
        self._stopping = asyncio.Event()
        self._last_error: str = ""
        self._last_error_code: int | None = None
        self._ib_connected: bool | None = None
        self._ib_connection_error: str = ""
        self._publish_retry_attempts = max(1, int(publish_retry_attempts))

    async def consume_forever(self) -> None:
        self._stopping.clear()
        while not self._stopping.is_set():
            await self._tick()

    def stop(self) -> None:
        self._stopping.set()

    async def _tick(self) -> None:
        try:
            item = await self._redis.blpop(self._queue_key, timeout=self._poll_timeout)
        except Exception as exc:  # pragma: no cover - redis failure
            await self._update_health(last_error=str(exc))
            await asyncio.sleep(self._retry_delay)
            return

        if item is None:
            await self._update_health()
            return

        _, raw_payload = item
        try:
            command = json.loads(raw_payload)
        except json.JSONDecodeError as exc:
            self._logger.error("Failed to decode command", extra={"error": str(exc), "payload": raw_payload})
            await self._update_health(last_error=str(exc))
            return

        command_id = str(command.get("command_id") or "")
        max_retries = int(command.get("max_retries", self._default_max_retries))
        attempts = 0
        last_error: str | None = None
        last_error_code: int | None = None

        while True:
            attempts += 1
            try:
                result = await self._executor.execute(command, attempts=attempts)
            except RetryableIBCommandError as exc:
                last_error = str(exc)
                last_error_code = getattr(exc, "error_code", None)
                if attempts > max_retries:
                    await self._publish_failure(
                        command_id, attempts, last_error, last_error_code
                    )
                    break
                self._logger.warning(
                    "Retryable IB command failure",
                    extra={
                        "command_id": command_id,
                        "attempt": attempts,
                        "max_retries": max_retries,
                        "error": last_error,
                    },
                )
                await asyncio.sleep(self._retry_delay)
                continue
            except IBCommandError as exc:
                last_error = str(exc)
                last_error_code = getattr(exc, "error_code", None)
                await self._publish_failure(
                    command_id, attempts, last_error, last_error_code
                )
                break
            except Exception as exc:  # pragma: no cover - defensive guard
                last_error = str(exc)
                last_error_code = getattr(exc, "error_code", None)
                await self._publish_failure(
                    command_id, attempts, last_error, last_error_code
                )
                break
            else:
                published = await self._publish_with_retries(
                    result,
                    command_id,
                )
                if published:
                    self._last_error = ""
                    self._last_error_code = None
                    await self._update_health()
                return

        await self._update_health(
            last_error=last_error,
            last_error_code=last_error_code,
        )

    async def _publish_failure(
        self,
        command_id: str,
        attempts: int,
        error: str | None,
        error_code: int | None,
    ) -> None:
        payload = ExecutionResult(
            command_id=command_id,
            status="failed",
            result=None,
            attempts=attempts,
            error=error,
            error_code=error_code,
        )
        await self._publish_with_retries(payload, command_id)

    async def _update_health(
        self,
        *,
        last_error: str | None = None,
        last_error_code: int | None = None,
    ) -> None:
        queue_depth = await self._safe_queue_depth()

        ib_connected, ib_error = self._executor.describe_connectivity()
        if ib_connected is not None:
            self._ib_connected = bool(ib_connected)
            if self._ib_connected:
                self._ib_connection_error = ""
        if self._ib_connected is False:
            if ib_error:
                self._ib_connection_error = ib_error.strip()
            elif not self._ib_connection_error:
                self._ib_connection_error = "ib connection unavailable"
        elif ib_error:
            # When we cannot determine connectivity but still receive an error,
            # surface it via the dedicated field.
            self._ib_connection_error = ib_error.strip()

        if last_error is not None:
            self._last_error = last_error or ""
        if last_error_code is not None:
            try:
                self._last_error_code = int(last_error_code)
            except (TypeError, ValueError):
                self._last_error_code = None

        reported_last_error = self._last_error or self._ib_connection_error
        values = {
            "last_seen": datetime.now(timezone.utc).isoformat(),
            "queue_depth": str(queue_depth),
            "last_error": reported_last_error,
            "last_error_code": ""
            if self._last_error_code is None
            else str(self._last_error_code),
        }
        if self._ib_connected is not None:
            values["ib_connected"] = "true" if self._ib_connected else "false"
        if self._ib_connection_error:
            values["ib_connection_error"] = self._ib_connection_error
        else:
            values["ib_connection_error"] = ""
        try:
            await self._redis.hset(self._health_key, mapping=values)
        except Exception:  # pragma: no cover - defensive guard
            self._logger.warning("Failed to update health information", extra={"values": values})

    async def _safe_queue_depth(self) -> int:
        try:
            return await get_queue_length(self._queue_key, redis=self._redis)
        except Exception:  # pragma: no cover - redis failure path
            return -1

    async def _publish_with_retries(
        self,
        payload: ExecutionResult,
        command_id: str,
    ) -> bool:
        """Attempt to publish a result, retrying on transient Redis errors."""

        attempts = 0
        while attempts < self._publish_retry_attempts and not self._stopping.is_set():
            attempts += 1
            try:
                await self._publisher.publish(payload)
            except asyncio.CancelledError:
                raise
            except RedisError as exc:
                self._logger.warning(
                    "Failed to publish IB command result",
                    extra={
                        "command_id": command_id,
                        "publish_attempt": attempts,
                        "error": str(exc),
                    },
                )
                await self._update_health(last_error=str(exc))
                if attempts >= self._publish_retry_attempts:
                    self._logger.error(
                        "Giving up publishing IB command result",
                        extra={
                            "command_id": command_id,
                            "attempts": attempts,
                            "error": str(exc),
                        },
                    )
                    return False
                await asyncio.sleep(self._retry_delay)
            except Exception as exc:  # pragma: no cover - defensive guard
                self._logger.error(
                    "Unexpected error publishing IB command result",
                    extra={
                        "command_id": command_id,
                        "attempts": attempts,
                        "error": str(exc),
                    },
                )
                await self._update_health(last_error=str(exc))
                return False
            else:
                if attempts > 1:
                    self._logger.info(
                        "Recovered after retrying result publication",
                        extra={
                            "command_id": command_id,
                            "attempts": attempts,
                        },
                    )
                return True

        return False
