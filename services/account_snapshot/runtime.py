"""Runtime orchestration for the standalone account snapshot service."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from loguru import logger

from ibbot.config import AccountSnapshotServiceConfig, load_account_snapshot_config
from ibbot.redis import (
    RedisSettings,
    close_redis,
    create_redis,
)
from src.account_snapshot_cache import mark_all_snapshots_stale
from src.account_snapshot_service import AccountSnapshotServiceClient
from src.utils import now_local

try:  # pragma: no cover - redis may be optional in tests
    from redis.asyncio import Redis
except Exception:  # pragma: no cover
    Redis = None  # type: ignore

__all__ = ["AccountSnapshotRuntime", "RuntimeStatus"]


@dataclass(slots=True)
class RuntimeStatus:
    """Expose runtime metrics for observability endpoints."""

    last_account_refresh: Optional[datetime]
    last_positions_refresh: Optional[datetime]
    last_fill: Optional[datetime]
    refresh_interval_seconds: int
    command_timeout_seconds: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "last_account_refresh": self.last_account_refresh.isoformat()
            if self.last_account_refresh
            else None,
            "last_positions_refresh": self.last_positions_refresh.isoformat()
            if self.last_positions_refresh
            else None,
            "last_fill": self.last_fill.isoformat() if self.last_fill else None,
            "refresh_interval_seconds": self.refresh_interval_seconds,
            "command_timeout_seconds": self.command_timeout_seconds,
        }


class AccountSnapshotRuntime:
    """Coordinate the background workers for refreshing account snapshots."""

    def __init__(
        self,
        *,
        config: Optional[AccountSnapshotServiceConfig] = None,
        redis_settings: Optional[RedisSettings] = None,
    ) -> None:
        self.config = config or load_account_snapshot_config()
        self._redis_settings = redis_settings or RedisSettings(url=self.config.redis_url)
        self._redis: Optional[Redis] = None
        self._stop_event = asyncio.Event()
        self._refresh_task: Optional[asyncio.Task[Any]] = None
        self._control_task: Optional[asyncio.Task[Any]] = None
        self._lock = asyncio.Lock()
        self._started = False

        self._last_account_refresh: Optional[datetime] = None
        self._last_positions_refresh: Optional[datetime] = None
        self._last_fill: Optional[datetime] = None

        self._client = AccountSnapshotServiceClient(
            config=self.config,
            redis_factory=self._ensure_redis,
        )

        self._logger = logger.bind(module="account_snapshot_runtime", request_id="-")

    async def _ensure_redis(self) -> Optional[Redis]:
        if self._redis is not None:
            return self._redis
        self._redis = await create_redis(settings=self._redis_settings)
        return self._redis

    async def startup(self) -> None:
        """Initialise the Redis connection and start background tasks."""

        async with self._lock:
            if self._started:
                raise RuntimeError("AccountSnapshotRuntime already started")

            redis = await self._ensure_redis()
            if redis is None:
                raise RuntimeError("Redis connection unavailable for account snapshot runtime")

            self._stop_event.clear()
            self._refresh_task = asyncio.create_task(self._refresh_loop())
            self._control_task = asyncio.create_task(self._control_loop())
            self._started = True
            self._logger.info(
                "Account snapshot runtime started",
                refresh_interval=self.config.refresh_interval_seconds,
                check_interval=self.config.check_interval_seconds,
            )

    async def shutdown(self) -> None:
        """Stop all background tasks and close connections."""

        async with self._lock:
            if not self._started:
                return

            self._stop_event.set()

            for task in (self._refresh_task, self._control_task):
                if task is None:
                    continue
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            self._refresh_task = None
            self._control_task = None

            if self._redis is not None:
                await close_redis(self._redis)
                self._redis = None

            self._started = False
            self._logger.info("Account snapshot runtime stopped")

    # ------------------------------------------------------------------
    # Public API

    async def refresh_account_info(self, *, reason: str = "manual") -> Optional[Dict[str, Any]]:
        result = await self._client.refresh_account_info(reason=reason)
        if result is not None:
            self._last_account_refresh = now_local()
        return result

    async def refresh_positions(
        self, *, reason: str = "manual", fast: bool = True
    ) -> Optional[Any]:
        result = await self._client.refresh_positions(reason=reason, fast=fast)
        if result is not None:
            self._last_positions_refresh = now_local()
        return result

    async def refresh_both(
        self, *, reason: str = "scheduled", fast_positions: bool = True
    ) -> Dict[str, Any]:
        result = await self._client.refresh_both(
            reason=reason, fast_positions=fast_positions
        )
        now = now_local()
        if result.get("account_info") is not None:
            self._last_account_refresh = now
        if result.get("positions") is not None:
            self._last_positions_refresh = now
        return result

    async def mark_stale(self, reason: str = "external") -> None:
        await mark_all_snapshots_stale(reason)
        self._last_account_refresh = None
        self._last_positions_refresh = None

    def status(self) -> RuntimeStatus:
        return RuntimeStatus(
            last_account_refresh=self._last_account_refresh,
            last_positions_refresh=self._last_positions_refresh,
            last_fill=self._last_fill,
            refresh_interval_seconds=self.config.refresh_interval_seconds,
            command_timeout_seconds=self.config.command_timeout_seconds,
        )

    # ------------------------------------------------------------------
    # Background loops

    async def _refresh_loop(self) -> None:
        if self.config.refresh_interval_seconds <= 0:
            self._logger.info("Periodic account snapshot refresh disabled")
            return

        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.config.check_interval_seconds,
                )
                break
            except asyncio.TimeoutError:
                pass

            if not self._should_refresh():
                continue

            try:
                await asyncio.wait_for(
                    self.refresh_both(reason="scheduled", fast_positions=True),
                    timeout=float(self.config.request_timeout_seconds),
                )
            except asyncio.TimeoutError:
                self._logger.warning("Scheduled account snapshot refresh timed out")
            except Exception as exc:  # pragma: no cover - defensive logging
                self._logger.warning(
                    "Scheduled account snapshot refresh failed",
                    error=str(exc),
                )

    def _should_refresh(self) -> bool:
        now = now_local()

        if self._last_fill is not None:
            since_fill = now - self._last_fill
            if since_fill < timedelta(seconds=self.config.refresh_interval_seconds):
                return False

        interval = timedelta(seconds=self.config.refresh_interval_seconds)
        account_due = (
            self._last_account_refresh is None
            or (now - self._last_account_refresh) >= interval
        )
        positions_due = (
            self._last_positions_refresh is None
            or (now - self._last_positions_refresh) >= interval
        )
        return account_due or positions_due

    async def _control_loop(self) -> None:
        redis = await self._ensure_redis()
        if redis is None:
            self._logger.warning("Control loop disabled (Redis unavailable)")
            return

        while not self._stop_event.is_set():
            try:
                entry = await redis.blpop(
                    self.config.control_queue_key, timeout=1
                )
            except asyncio.CancelledError:  # pragma: no cover - normal shutdown
                raise
            except Exception as exc:
                self._logger.warning(
                    "Control queue read failed", error=str(exc)
                )
                await asyncio.sleep(1.0)
                continue

            if entry is None:
                continue

            _, raw = entry
            try:
                payload = json.loads(raw)
            except Exception:
                self._logger.debug("Discarding malformed control payload", raw=raw)
                continue

            message_type = payload.get("type")
            if message_type == "fill":
                self._last_fill = now_local()
            elif message_type == "refresh":
                reason = payload.get("reason", "control")
                fast = bool(payload.get("fast_positions", True))
                await self.refresh_both(reason=reason, fast_positions=fast)
            elif message_type == "refresh_account_info":
                reason = payload.get("reason", "control")
                await self.refresh_account_info(reason=reason)
            elif message_type == "refresh_positions":
                reason = payload.get("reason", "control")
                fast = bool(payload.get("fast_positions", True))
                await self.refresh_positions(reason=reason, fast=fast)
            elif message_type == "mark_stale":
                reason = payload.get("reason", "control")
                await self.mark_stale(reason)
            else:
                self._logger.debug(
                    "Unknown control message", payload=payload
                )

