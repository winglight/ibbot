"""Background service for refreshing account snapshots in Redis."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from loguru import logger

from .account_snapshot_cache import (
    mark_all_snapshots_stale,
    write_account_info_snapshot,
    write_positions_snapshot,
)
from .utils import now_local


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
            except Exception as exc:  # pragma: no cover - defensive guard
                snapshot_service_logger.warning(
                    f"Failed to mark snapshots stale: {exc}"
                )

        def _schedule_mark() -> None:
            task = loop.create_task(_mark())

            def _log_error(fut: asyncio.Future) -> None:
                try:
                    fut.result()
                except asyncio.CancelledError:
                    pass
                except Exception as exc:  # pragma: no cover - defensive guard
                    snapshot_service_logger.warning(
                        f"Failed to mark snapshots stale: {exc}"
                    )

            task.add_done_callback(_log_error)

        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            loop.call_soon_threadsafe(_schedule_mark)
        else:
            if running_loop is loop:
                _schedule_mark()
            else:
                loop.call_soon_threadsafe(_schedule_mark)

    def request_immediate_refresh(self, reason: str = "connection_restored") -> None:
        """Schedule an immediate refresh when the connection becomes available."""

        loop = self._loop
        if not loop or not loop.is_running():
            return

        coro = self.refresh_both(reason=reason, fast_positions=True)

        def _schedule_refresh() -> None:
            loop.create_task(coro)

        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            loop.call_soon_threadsafe(_schedule_refresh)
        else:
            if running_loop is loop:
                _schedule_refresh()
            else:
                loop.call_soon_threadsafe(_schedule_refresh)

    # ------------------------------------------------------------------
    # Refresh helpers

    async def refresh_account_info(self, reason: str = "manual") -> Optional[Dict[str, Any]]:
        """Fetch account info from IBKR and persist it in Redis."""

        ib_client = _get_ib_client()
        if ib_client is None:
            return None

        info = await ib_client.get_account_info()
        if info is not None:
            await write_account_info_snapshot(info, reason)
            self._last_account_refresh = now_local()
        return info

    async def refresh_positions(
        self, *, reason: str = "manual", fast: bool = True
    ) -> Optional[Any]:
        """Fetch positions from IBKR and persist them in Redis."""

        ib_client = _get_ib_client()
        if ib_client is None:
            return None

        positions = await ib_client.get_positions(fast=fast)
        await write_positions_snapshot(positions, reason)
        self._last_positions_refresh = now_local()
        return positions

    async def refresh_both(
        self, *, reason: str = "scheduled", fast_positions: bool = True
    ) -> Dict[str, Any]:
        """Refresh both account info and positions in sequence."""

        ib_client = _get_ib_client()
        if ib_client is None:
            return {"account_info": None, "positions": None}

        info = await ib_client.get_account_info()
        positions = await ib_client.get_positions(fast=fast_positions)

        await write_account_info_snapshot(info, reason)
        await write_positions_snapshot(positions, reason)

        now = now_local()
        self._last_account_refresh = now
        self._last_positions_refresh = now
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
            except Exception as exc:
                snapshot_service_logger.warning(
                    f"Scheduled account snapshot refresh failed: {exc}"
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


def _get_ib_client():
    """Lazy import helper to avoid circular dependencies."""

    try:
        import core  # Local import to avoid circular dependency at module load

        return getattr(core, "ibkr_client", None)
    except Exception:  # pragma: no cover - defensive guard
        return None
