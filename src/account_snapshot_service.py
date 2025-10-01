"""Background service for refreshing account snapshots in Redis."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta
from typing import Any, Dict, Mapping, Optional

from loguru import logger

from .account_snapshot_cache import (
    mark_all_snapshots_stale,
    write_account_info_snapshot,
    write_positions_snapshot,
)
from .utils import now_local, to_local_iso
from src.ibbot import update_service_health
from src.ibbot.redis import get_redis_client


def _read_interval_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return max(0, default)
    return max(0, value)


snapshot_service_logger = logger.bind(module="account_snapshot_service", request_id="-")


class AccountSnapshotService:
    """Maintain Redis account snapshots via scheduled refreshes."""

    def __init__(self) -> None:
        self.refresh_interval_seconds = _read_interval_env(
            "ACCOUNT_SNAPSHOT_REFRESH_INTERVAL_SECONDS", 300
        )
        # Polling frequency for checking if a refresh is necessary.
        default_poll = min(60, max(15, self.refresh_interval_seconds // 2 or 30))
        self._poll_interval_seconds = _read_interval_env(
            "ACCOUNT_SNAPSHOT_CHECK_INTERVAL_SECONDS", default_poll
        ) or default_poll
        self._request_timeout_seconds = _read_interval_env(
            "ACCOUNT_SNAPSHOT_REFRESH_TIMEOUT_SECONDS", 15
        ) or 15

        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._last_fill: Optional[datetime] = None
        self._last_account_refresh: Optional[datetime] = None
        self._last_positions_refresh: Optional[datetime] = None
        self._health_redis: Any = None
        self._health_last_error: str = ""

    async def _ensure_health_client(self) -> Any | None:
        if self._health_redis is not None:
            return self._health_redis
        try:
            self._health_redis = await get_redis_client()
        except Exception as exc:
            snapshot_service_logger.warning(
                "Failed to acquire Redis client for health updates: {}", exc
            )
            self._health_redis = None
        return self._health_redis

    def _build_health_metrics(self, extra: Mapping[str, Any] | None = None) -> Dict[str, Any]:
        metrics: Dict[str, Any] = {
            "last_account_refresh": to_local_iso(self._last_account_refresh) or "",
            "last_positions_refresh": to_local_iso(self._last_positions_refresh) or "",
        }
        if extra:
            metrics.update(extra)
        return metrics

    async def _update_health(
        self,
        *,
        healthy: bool,
        last_error: str | None = None,
        extra_metrics: Mapping[str, Any] | None = None,
    ) -> None:
        client = await self._ensure_health_client()
        if client is None:
            return
        if healthy:
            self._health_last_error = ""
        elif last_error is not None:
            self._health_last_error = last_error
        metrics = self._build_health_metrics(extra_metrics)
        await update_service_health(
            client,
            "account-snapshot",
            healthy=healthy,
            last_error=self._health_last_error,
            metrics=metrics,
            logger=snapshot_service_logger,
        )

    # ------------------------------------------------------------------
    # Lifecycle management

    def start(self) -> None:
        """Start the background refresh loop if it is not running."""

        if self._task and not self._task.done():
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            snapshot_service_logger.warning(
                "AccountSnapshotService.start() requires a running event loop"
            )
            return

        self._loop = loop
        self._stop_event = asyncio.Event()
        self._task = loop.create_task(self._run_loop())
        snapshot_service_logger.info(
            "Account snapshot service started (refresh_interval={}s, poll={}s)",
            self.refresh_interval_seconds,
            self._poll_interval_seconds,
        )

    async def stop(self) -> None:
        """Stop the background loop."""

        if self._stop_event:
            self._stop_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:  # pragma: no cover - expected cancellation path
                pass
            except Exception as exc:  # pragma: no cover - defensive guard
                snapshot_service_logger.warning(
                    f"Account snapshot service stop raised: {exc}"
                )
        self._task = None
        self._stop_event = None

    # ------------------------------------------------------------------
    # External signals

    def notify_fill(self) -> None:
        """Record the timestamp of the most recent fill."""

        self._last_fill = now_local()

    def flag_connection_loss(self, reason: str) -> None:
        """Mark cached snapshots as stale after a connection loss."""

        loop = self._loop
        if not loop or not loop.is_running():
            snapshot_service_logger.debug(
                "Skip flagging snapshots as stale (loop unavailable)",
            )
            return

        async def _mark():
            try:
                await mark_all_snapshots_stale(reason)
                self._last_account_refresh = None
                self._last_positions_refresh = None
                await self._update_health(
                    healthy=False,
                    last_error=reason,
                    extra_metrics={"state": "stale", "stale_reason": reason},
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                snapshot_service_logger.warning(
                    f"Failed to mark snapshots stale: {exc}"
                )
                await self._update_health(
                    healthy=False,
                    last_error=str(exc),
                    extra_metrics={"state": "error", "stale_reason": reason},
                )

        asyncio.run_coroutine_threadsafe(_mark(), loop)

    def request_immediate_refresh(self, reason: str = "connection_restored") -> None:
        """Schedule an immediate refresh when the connection becomes available."""

        loop = self._loop
        if not loop or not loop.is_running():
            return

        asyncio.run_coroutine_threadsafe(
            self.refresh_both(reason=reason, fast_positions=True),
            loop,
        )

    # ------------------------------------------------------------------
    # Refresh helpers

    async def refresh_account_info(self, reason: str = "manual") -> Optional[Dict[str, Any]]:
        """Fetch account info from IBKR and persist it in Redis."""

        ib_client = _get_ib_client()
        if ib_client is None:
            await self._update_health(
                healthy=False,
                last_error="IB client unavailable",
                extra_metrics={"reason": reason},
            )
            return None

        try:
            info = await ib_client.get_account_info()
        except Exception as exc:
            await self._update_health(
                healthy=False,
                last_error=str(exc),
                extra_metrics={"reason": reason},
            )
            raise
        if info is None:
            await self._update_health(
                healthy=False,
                last_error="Account info unavailable",
                extra_metrics={"reason": reason, "scope": "account_info"},
            )
            return None

        persisted = await write_account_info_snapshot(info, reason)
        if persisted:
            refreshed_at = now_local()
            self._last_account_refresh = refreshed_at
            self._log_snapshot_update(
                "account_info", reason=reason, refreshed_at=refreshed_at
            )
            await self._update_health(
                healthy=True,
                extra_metrics={"reason": reason, "scope": "account_info"},
            )
        else:
            await self._update_health(
                healthy=False,
                last_error="Failed to persist account snapshot",
                extra_metrics={"reason": reason, "scope": "account_info"},
            )
        return info

    async def refresh_positions(
        self, *, reason: str = "manual", fast: bool = True
    ) -> Optional[Any]:
        """Fetch positions from IBKR and persist them in Redis."""

        ib_client = _get_ib_client()
        if ib_client is None:
            await self._update_health(
                healthy=False,
                last_error="IB client unavailable",
                extra_metrics={"reason": reason, "scope": "positions"},
            )
            return None

        try:
            positions = await ib_client.get_positions(fast=fast)
        except Exception as exc:
            await self._update_health(
                healthy=False,
                last_error=str(exc),
                extra_metrics={"reason": reason, "scope": "positions"},
            )
            raise
        persisted = await write_positions_snapshot(positions, reason)
        if persisted:
            refreshed_at = now_local()
            self._last_positions_refresh = refreshed_at
            self._log_snapshot_update(
                "positions", reason=reason, refreshed_at=refreshed_at
            )
            await self._update_health(
                healthy=True,
                extra_metrics={"reason": reason, "scope": "positions"},
            )
        else:
            await self._update_health(
                healthy=False,
                last_error="Failed to persist positions snapshot",
                extra_metrics={"reason": reason, "scope": "positions"},
            )
        return positions

    async def refresh_both(
        self, *, reason: str = "scheduled", fast_positions: bool = True
    ) -> Dict[str, Any]:
        """Refresh both account info and positions in sequence."""

        ib_client = _get_ib_client()
        if ib_client is None:
            await self._update_health(
                healthy=False,
                last_error="IB client unavailable",
                extra_metrics={"reason": reason, "scope": "both"},
            )
            return {"account_info": None, "positions": None}

        try:
            info = await ib_client.get_account_info()
            positions = await ib_client.get_positions(fast=fast_positions)
        except Exception as exc:
            await self._update_health(
                healthy=False,
                last_error=str(exc),
                extra_metrics={"reason": reason, "scope": "both"},
            )
            raise

        info_persisted = await write_account_info_snapshot(info, reason)
        positions_persisted = await write_positions_snapshot(positions, reason)

        now = now_local()
        if info_persisted:
            self._last_account_refresh = now
            self._log_snapshot_update("account_info", reason=reason, refreshed_at=now)
        if positions_persisted:
            self._last_positions_refresh = now
            self._log_snapshot_update("positions", reason=reason, refreshed_at=now)

        if info_persisted and positions_persisted:
            await self._update_health(
                healthy=True,
                extra_metrics={"reason": reason, "scope": "both"},
            )
        else:
            failure_parts = []
            if not info_persisted:
                failure_parts.append("account info")
            if not positions_persisted:
                failure_parts.append("positions")
            description = ", ".join(failure_parts) + " snapshot persistence failed"
            await self._update_health(
                healthy=False,
                last_error=description,
                extra_metrics={"reason": reason, "scope": "both"},
            )
        return {"account_info": info, "positions": positions}

    # ------------------------------------------------------------------
    # Internal helpers

    async def _run_loop(self) -> None:
        """Periodically refresh snapshots while the service is running."""

        if self.refresh_interval_seconds <= 0:
            snapshot_service_logger.info(
                "Account snapshot periodic refresh disabled via configuration"
            )
            return

        while self._stop_event and not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._poll_interval_seconds,
                )
                break
            except asyncio.TimeoutError:
                pass

            if not self._should_refresh():
                await self._update_health(
                    healthy=True,
                    extra_metrics={"reason": "scheduled", "state": "idle"},
                )
                continue

            try:
                await asyncio.wait_for(
                    self.refresh_both(reason="scheduled", fast_positions=True),
                    timeout=self._request_timeout_seconds,
                )
            except asyncio.TimeoutError:
                snapshot_service_logger.warning(
                    "Scheduled account snapshot refresh timed out"
                )
                await self._update_health(
                    healthy=False,
                    last_error="Scheduled refresh timed out",
                    extra_metrics={"reason": "scheduled", "state": "timeout"},
                )
            except Exception as exc:
                snapshot_service_logger.warning(
                    f"Scheduled account snapshot refresh failed: {exc}"
                )
                await self._update_health(
                    healthy=False,
                    last_error=str(exc),
                    extra_metrics={"reason": "scheduled", "state": "error"},
                )

    def _should_refresh(self) -> bool:
        """Return whether a scheduled refresh should be executed now."""

        now = now_local()

        if self._last_fill:
            since_fill = now - self._last_fill
            if since_fill < timedelta(seconds=self.refresh_interval_seconds):
                return False

        account_due = (
            self._last_account_refresh is None
            or (now - self._last_account_refresh)
            >= timedelta(seconds=self.refresh_interval_seconds)
        )
        positions_due = (
            self._last_positions_refresh is None
            or (now - self._last_positions_refresh)
            >= timedelta(seconds=self.refresh_interval_seconds)
        )
        return account_due or positions_due

    # ------------------------------------------------------------------
    # Logging helpers

    def _next_refresh_display(self, reference: Optional[datetime] = None) -> str:
        """Return a human-readable timestamp for the next planned refresh."""

        if self.refresh_interval_seconds <= 0:
            return "disabled"

        base = reference or now_local()
        next_refresh = base + timedelta(seconds=self.refresh_interval_seconds)
        return to_local_iso(next_refresh) or "unknown"

    def _log_snapshot_update(
        self, scope: str, *, reason: str, refreshed_at: Optional[datetime]
    ) -> None:
        """Emit a structured log message after refreshing a snapshot."""

        refreshed_text = to_local_iso(refreshed_at) if refreshed_at else "unknown"
        next_refresh = self._next_refresh_display(refreshed_at)
        snapshot_service_logger.info(
            "{} snapshot updated (reason={}, refreshed_at={}, next_refresh={})",
            scope,
            reason,
            refreshed_text,
            next_refresh,
        )


def _get_ib_client():
    """Lazy import helper to avoid circular dependencies."""

    try:
        import core  # Local import to avoid circular dependency at module load

        return getattr(core, "ibkr_client", None)
    except Exception:  # pragma: no cover - defensive guard
        return None
