"""Account microservice exposing account info and positions via FastAPI."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, Mapping, MutableMapping

from fastapi import Depends, FastAPI, HTTPException, Query

from fastapi_workers import WorkerQueue

from src.account_snapshot_cache import (
    read_account_info_snapshot,
    read_positions_snapshot,
    write_account_info_snapshot,
    write_positions_snapshot,
)
from src.ibbot import update_service_health
from src.ibbot.logging import get_logger
from src.ibbot.redis import DEFAULT_COMMAND_QUEUE, get_redis_client, enqueue_ib_command
from src.utils import now_local, to_local_iso


RefreshWriter = Callable[[Any, str], Awaitable[bool]]


async def _default_redis_factory() -> Any:
    redis_url = os.getenv("ACCOUNT_SERVICE_REDIS_URL", "").strip()
    return await get_redis_client(redis_url or None)


def _decode_result(payload: Mapping[str, Any]) -> Any:
    raw = payload.get("result")
    if raw in (None, "", "null"):
        return None
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="ignore")
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


class RefreshError(RuntimeError):
    """Raised when a refresh operation fails."""


@dataclass(slots=True)
class AccountServiceRuntime:
    """Coordinate snapshot reads and refreshes for account data."""

    redis_factory: Callable[[], Awaitable[Any]] = field(default=_default_redis_factory)
    command_queue: str = DEFAULT_COMMAND_QUEUE
    result_hash_prefix: str = "hash:ib:result"
    refresh_timeout: float = float(os.getenv("ACCOUNT_SERVICE_COMMAND_TIMEOUT", "12.0"))
    poll_interval: float = float(os.getenv("ACCOUNT_SERVICE_POLL_INTERVAL", "0.2"))
    health_interval: float = float(os.getenv("ACCOUNT_SERVICE_HEALTH_INTERVAL", "10.0"))
    logger_name: str = "ibbot.services.account"
    worker: WorkerQueue = field(default_factory=lambda: WorkerQueue(name="account-service"))

    _redis: Any | None = field(default=None, init=False, repr=False)
    _logger: Any | None = field(default=None, init=False, repr=False)
    _health_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _stop: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _info_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _positions_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _last_error: str = field(default="", init=False, repr=False)
    _last_account_refresh: datetime | None = field(default=None, init=False, repr=False)
    _last_positions_refresh: datetime | None = field(default=None, init=False, repr=False)

    async def start(self) -> None:
        self._logger = get_logger(self.logger_name)
        self._redis = await self.redis_factory()
        self._stop.clear()
        self._health_task = asyncio.create_task(self._health_loop(), name="account-service-health")

    async def stop(self) -> None:
        self._stop.set()
        if self._health_task is not None:
            self._health_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._health_task
            self._health_task = None
        await self.worker.shutdown(cancel=True)
        self._redis = None

    async def _health_loop(self) -> None:
        while not self._stop.is_set():
            await self._publish_health()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.health_interval)
            except asyncio.TimeoutError:
                continue

    async def _publish_health(self, *, healthy: bool | None = None, last_error: str | None = None) -> None:
        redis = self._redis
        if redis is None:
            return
        metrics: Dict[str, Any] = {
            "last_account_refresh": to_local_iso(self._last_account_refresh) or "",
            "last_positions_refresh": to_local_iso(self._last_positions_refresh) or "",
        }
        resolved_error = (
            last_error if last_error is not None else self._last_error or None
        )
        if resolved_error == "":
            resolved_error = None
        await update_service_health(
            redis,
            "account-service",
            healthy=True if healthy is None else healthy,
            last_error=resolved_error,
            metrics=metrics,
            logger=self._logger,
        )

    async def _record_success(self, scope: str) -> None:
        now = now_local()
        if scope == "account_info":
            self._last_account_refresh = now
        elif scope == "positions":
            self._last_positions_refresh = now
        self._last_error = ""
        await self._publish_health(healthy=True, last_error="")

    async def _record_failure(self, error: str) -> None:
        self._last_error = error
        await self._publish_health(healthy=False, last_error=error)

    async def _wait_for_result(self, command_id: str) -> Mapping[str, Any] | None:
        redis = self._redis
        if redis is None:
            raise RefreshError("Redis unavailable")
        key = f"{self.result_hash_prefix}:{command_id}"
        deadline = time.monotonic() + max(0.5, float(self.refresh_timeout))
        while True:
            payload: MutableMapping[str, Any] = await redis.hgetall(key)
            if payload and payload.get("status"):
                return payload
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(self.poll_interval)

    async def _refresh(
        self,
        *,
        method: str,
        writer: RefreshWriter,
        source: str,
        kwargs: Mapping[str, Any] | None = None,
    ) -> tuple[Any | None, str]:
        redis = self._redis
        if redis is None:
            raise RefreshError("Redis unavailable")
        command_id = uuid.uuid4().hex
        try:
            payload = {
                "command_id": command_id,
                "method": method,
                "kwargs": dict(kwargs or {}),
            }
            await enqueue_ib_command(payload, redis=redis, queue_key=self.command_queue)
            result_payload = await self._wait_for_result(command_id)
            if not result_payload:
                raise RefreshError("Timed out waiting for IB command result")
            status = str(result_payload.get("status") or "").lower()
            if status != "success":
                error = result_payload.get("error") or "unknown-error"
                raise RefreshError(str(error))
            data = _decode_result(result_payload)
            if data is not None:
                async def _persist() -> None:
                    try:
                        await writer(data, source)
                    except Exception as exc:  # pragma: no cover - defensive logging
                        if self._logger is not None:
                            self._logger.warning(
                                "Failed to persist %s snapshot", method, extra={"error": str(exc)}
                            )

                await self.worker.spawn(_persist())
            return data, command_id
        except RefreshError:
            raise
        except Exception as exc:  # pragma: no cover - defensive guard
            raise RefreshError(str(exc)) from exc

    async def get_account_info(self, *, force_refresh: bool = False) -> Mapping[str, Any]:
        snapshot = await read_account_info_snapshot()
        should_refresh = force_refresh or not snapshot or bool(snapshot.get("stale"))
        command_id: str | None = None
        from_cache = True
        error: str | None = None
        refreshed_data: Any | None = None

        if should_refresh:
            async with self._info_lock:
                try:
                    refreshed_data, command_id = await self._refresh(
                        method="get_account_info",
                        writer=write_account_info_snapshot,
                        source="account_service",
                    )
                    if refreshed_data is not None:
                        from_cache = False
                        await self._record_success("account_info")
                except RefreshError as exc:
                    error = str(exc)
                    await self._record_failure(error)
        if refreshed_data is None:
            snapshot = await read_account_info_snapshot()
        else:
            snapshot = await read_account_info_snapshot()

        if refreshed_data is None:
            if snapshot is None:
                raise HTTPException(status_code=503, detail="Account info unavailable")
            warning = error or ("stale" if snapshot.get("stale") else None)
            response = self._build_snapshot_response(
                snapshot,
                data_key="account_info",
                from_cache=True,
                warning=warning,
            )
            if error:
                response["error"] = error
            return response

        response = self._build_snapshot_response(
            snapshot,
            data_key="account_info",
            from_cache=from_cache,
            data_override=refreshed_data,
        )
        if command_id:
            response["command_id"] = command_id
        return response

    async def get_positions(
        self,
        *,
        fast: bool = True,
        force_refresh: bool = False,
    ) -> Mapping[str, Any]:
        snapshot = await read_positions_snapshot()
        should_refresh = force_refresh or not snapshot or bool(snapshot.get("stale"))
        command_id: str | None = None
        refreshed_data: Any | None = None
        error: str | None = None

        if should_refresh:
            async with self._positions_lock:
                try:
                    refreshed_data, command_id = await self._refresh(
                        method="get_positions",
                        writer=write_positions_snapshot,
                        source="account_service",
                        kwargs={"fast": bool(fast)},
                    )
                    if refreshed_data is not None:
                        await self._record_success("positions")
                except RefreshError as exc:
                    error = str(exc)
                    await self._record_failure(error)
        if refreshed_data is None:
            snapshot = await read_positions_snapshot()
        else:
            snapshot = await read_positions_snapshot()

        if refreshed_data is None:
            if snapshot is None:
                raise HTTPException(status_code=503, detail="Positions unavailable")
            warning = error or ("stale" if snapshot.get("stale") else None)
            response = self._build_snapshot_response(
                snapshot,
                data_key="positions",
                from_cache=True,
                default=[],
                warning=warning,
            )
            if error:
                response["error"] = error
            return response

        response = self._build_snapshot_response(
            snapshot,
            data_key="positions",
            from_cache=False,
            data_override=refreshed_data,
            default=[],
        )
        if command_id:
            response["command_id"] = command_id
        return response

    def _build_snapshot_response(
        self,
        snapshot: Mapping[str, Any] | None,
        *,
        data_key: str,
        from_cache: bool,
        warning: str | None = None,
        data_override: Any | None = None,
        default: Any | None = None,
    ) -> Dict[str, Any]:
        payload = snapshot.get("data") if snapshot else None
        data = data_override if data_override is not None else payload
        if data is None:
            data = default
        response: Dict[str, Any] = {
            "success": True,
            data_key: data,
            "from_cache": bool(from_cache),
            "cache_updated_at": snapshot.get("updated_at") if snapshot else None,
            "cache_source": snapshot.get("source") if snapshot else None,
            "stale": bool(snapshot.get("stale")) if snapshot else False,
        }
        if warning:
            response["warning"] = warning
        if snapshot:
            for field in ("stale_reason", "stale_at"):
                if snapshot.get(field) is not None:
                    response[field] = snapshot.get(field)
        return response


async def get_runtime() -> AccountServiceRuntime:
    if not hasattr(get_runtime, "_instance"):
        get_runtime._instance = AccountServiceRuntime()
    return get_runtime._instance  # type: ignore[attr-defined]


def create_app(runtime: AccountServiceRuntime | None = None) -> FastAPI:
    if runtime is None:

        @asynccontextmanager
        async def _lifespan(_: FastAPI):
            service = await get_runtime()
            await service.start()
            try:
                yield
            finally:
                await service.stop()

        async def _runtime_dependency() -> AccountServiceRuntime:
            return await get_runtime()

        app = FastAPI(title="Account Service", lifespan=_lifespan)
    else:

        @asynccontextmanager
        async def _lifespan(_: FastAPI):
            await runtime.start()
            try:
                yield
            finally:
                await runtime.stop()

        async def _runtime_dependency() -> AccountServiceRuntime:
            return runtime

        app = FastAPI(title="Account Service", lifespan=_lifespan)

    @app.get("/health")
    async def health(runtime: AccountServiceRuntime = Depends(_runtime_dependency)) -> Dict[str, Any]:
        return {
            "service": "account",
            "last_account_refresh": to_local_iso(runtime._last_account_refresh) or "",
            "last_positions_refresh": to_local_iso(runtime._last_positions_refresh) or "",
            "healthy": not runtime._last_error,
            "last_error": runtime._last_error or None,
        }

    @app.get("/account/info")
    async def account_info(
        force_refresh: bool = Query(False, description="Force IB refresh"),
        runtime: AccountServiceRuntime = Depends(_runtime_dependency),
    ) -> Mapping[str, Any]:
        return await runtime.get_account_info(force_refresh=force_refresh)

    @app.get("/account/positions")
    async def positions(
        fast: bool = Query(True, description="Use fast cached mode"),
        force_refresh: bool = Query(False, description="Force IB refresh"),
        runtime: AccountServiceRuntime = Depends(_runtime_dependency),
    ) -> Mapping[str, Any]:
        return await runtime.get_positions(fast=fast, force_refresh=force_refresh)

    return app


def main() -> None:
    host = os.getenv("ACCOUNT_SERVICE_HOST", "0.0.0.0")
    port = int(os.getenv("ACCOUNT_SERVICE_PORT", "8020"))
    import uvicorn

    uvicorn.run("services.account.main:create_app", host=host, port=port, factory=True)


if __name__ == "__main__":  # pragma: no cover - CLI execution
    main()
