"""Client helpers for interacting with the account snapshot service."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable, Coroutine, Dict, Optional

from loguru import logger

from ibbot.config import AccountSnapshotServiceConfig, load_account_snapshot_config
from ibbot.redis import ensure_redis, request_ib_command

from .account_snapshot_cache import (
    mark_all_snapshots_stale,
    write_account_info_snapshot,
    write_positions_snapshot,
)
from .utils import now_local

try:  # pragma: no cover - redis may be optional in some environments
    from redis.asyncio import Redis
except Exception:  # pragma: no cover
    Redis = None  # type: ignore

snapshot_service_logger = logger.bind(module="account_snapshot_client", request_id="-")


class AccountSnapshotServiceClient:
    """Thin client for requesting account snapshots via Redis queues."""

    def __init__(
        self,
        *,
        config: Optional[AccountSnapshotServiceConfig] = None,
        redis_factory: Optional[Callable[[], Awaitable[Optional[Redis]]]] = None,
    ) -> None:
        self.config = config or load_account_snapshot_config()
        self._redis_factory = redis_factory or self._default_redis_factory
        self._logger = snapshot_service_logger

    async def _default_redis_factory(self) -> Optional[Redis]:
        return await ensure_redis(url=self.config.redis_url)

    async def _get_redis(self) -> Optional[Redis]:
        try:
            redis = await self._redis_factory()
        except Exception as exc:  # pragma: no cover - defensive logging
            self._logger.warning("Redis factory failed", error=str(exc))
            return None
        return redis

    async def refresh_account_info(self, *, reason: str = "manual") -> Optional[Dict[str, Any]]:
        """Request account information from IB via the command queue."""

        redis = await self._get_redis()
        result = await request_ib_command(
            redis,
            "get_account_info",
            {"reason": reason},
            queue_key=self.config.command_queue_key,
            result_prefix=self.config.command_result_prefix,
            timeout=float(self.config.command_timeout_seconds),
            result_ttl=self.config.command_result_ttl_seconds,
            source=self.config.service_name,
        )
        if not result:
            return None

        payload = result.get("data") or result.get("result") or result
        if isinstance(payload, dict):
            await write_account_info_snapshot(payload, reason)
            return payload
        self._logger.debug("Account info command returned unexpected payload", payload=payload)
        return None

    async def refresh_positions(
        self, *, reason: str = "manual", fast: bool = True
    ) -> Optional[Any]:
        """Request positions from IB via the command queue."""

        redis = await self._get_redis()
        result = await request_ib_command(
            redis,
            "get_positions",
            {"reason": reason, "fast": fast},
            queue_key=self.config.command_queue_key,
            result_prefix=self.config.command_result_prefix,
            timeout=float(self.config.command_timeout_seconds),
            result_ttl=self.config.command_result_ttl_seconds,
            source=self.config.service_name,
        )
        if not result:
            return None

        payload = result.get("data") or result.get("result") or result
        await write_positions_snapshot(payload, reason)
        return payload

    async def refresh_both(
        self, *, reason: str = "scheduled", fast_positions: bool = True
    ) -> Dict[str, Any]:
        """Refresh both account info and positions sequentially."""

        info = await self.refresh_account_info(reason=reason)
        positions = await self.refresh_positions(reason=reason, fast=fast_positions)
        return {"account_info": info, "positions": positions}

    # ------------------------------------------------------------------
    # Control helpers

    def notify_fill(self) -> None:
        """Record a fill event to throttle periodic refreshes."""

        self._fire_and_forget(self._push_control_message({"type": "fill"}))

    def flag_connection_loss(self, reason: str) -> None:
        """Mark snapshots as stale and notify the background worker."""

        self._fire_and_forget(
            self._handle_connection_loss(reason)
        )

    async def _handle_connection_loss(self, reason: str) -> None:
        await mark_all_snapshots_stale(reason)
        await self._push_control_message({"type": "mark_stale", "reason": reason})

    def request_immediate_refresh(self, reason: str = "manual", *, fast_positions: bool = True) -> None:
        """Request the background worker to refresh snapshots immediately."""

        message = {
            "type": "refresh",
            "reason": reason,
            "fast_positions": fast_positions,
        }
        self._fire_and_forget(self._push_control_message(message))

    async def _push_control_message(self, payload: Dict[str, Any]) -> None:
        redis = await self._get_redis()
        if redis is None:
            return

        message = dict(payload)
        message.setdefault("source", self.config.service_name)
        message.setdefault("timestamp", now_local().isoformat())

        try:
            await redis.rpush(
                self.config.control_queue_key,
                json.dumps(message, ensure_ascii=False),
            )
        except Exception as exc:
            self._logger.warning(
                "Failed to push control message",
                payload=payload,
                error=str(exc),
            )

    def _fire_and_forget(self, coro: Coroutine[Any, Any, Any]) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(coro)
            return

        loop.create_task(coro)


account_snapshot_service = AccountSnapshotServiceClient()

__all__ = [
    "AccountSnapshotServiceClient",
    "account_snapshot_service",
]
