"""Background service for refreshing account snapshots in Redis."""

from __future__ import annotations

import asyncio
import os
import time
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
from src.ibbot.redis import (
    IBCommandExecutionError,
    IBCommandTimeout,
    get_redis_client,
    request_ib_command,
)


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
        """Fetch account info via the IB command bridge and persist the snapshot."""

        outcome = await self._execute_ib_command(
            "get_account_info",
            reason=reason,
            scope="account_info",
        )

        if outcome["status"] != "success":
            return None

        info = outcome["result"]
        if info is None:
            await self._update_health(
                healthy=False,
                last_error="Account info unavailable",
                extra_metrics={
                    "reason": reason,
                    "scope": "account_info",
                    "command_id": outcome["command_id"],
                    "source": IB_COMMAND_SOURCE,
                },
            )
            return None
        persisted = await write_account_info_snapshot(info, IB_COMMAND_SOURCE)
        if persisted:
            refreshed_at = now_local()
            self._last_account_refresh = refreshed_at
            self._log_snapshot_update(
                "account_info",
                reason=reason,
                refreshed_at=refreshed_at,
                source=IB_COMMAND_SOURCE,
            )
            await self._update_health(
                healthy=True,
                extra_metrics={
                    "reason": reason,
                    "scope": "account_info",
                    "command_id": outcome["command_id"],
                    "source": IB_COMMAND_SOURCE,
                    "duration": f"{outcome['duration']:.3f}",
                },
            )
        else:
            await self._update_health(
                healthy=False,
                last_error="Failed to persist account snapshot",
                extra_metrics={
                    "reason": reason,
                    "scope": "account_info",
                    "command_id": outcome["command_id"],
                    "source": IB_COMMAND_SOURCE,
                },
            )
        return info

    async def refresh_positions(
        self, *, reason: str = "manual", fast: bool = True
    ) -> Optional[Any]:
        """Fetch positions from IBKR and persist them in Redis."""

        outcome = await self._execute_ib_command(
            "get_positions",
            reason=reason,
            scope="positions",
            kwargs={"fast": fast},
        )

        if outcome["status"] != "success":
            return None

        positions = outcome["result"]
        if positions is None:
            await self._update_health(
                healthy=False,
                last_error="Positions unavailable",
                extra_metrics={
                    "reason": reason,
                    "scope": "positions",
                    "command_id": outcome["command_id"],
                    "source": IB_COMMAND_SOURCE,
                },
            )
            return None
        persisted = await write_positions_snapshot(positions, IB_COMMAND_SOURCE)
        if persisted:
            refreshed_at = now_local()
            self._last_positions_refresh = refreshed_at
            self._log_snapshot_update(
                "positions",
                reason=reason,
                refreshed_at=refreshed_at,
                source=IB_COMMAND_SOURCE,
            )
            await self._update_health(
                healthy=True,
                extra_metrics={
                    "reason": reason,
                    "scope": "positions",
                    "command_id": outcome["command_id"],
                    "source": IB_COMMAND_SOURCE,
                    "duration": f"{outcome['duration']:.3f}",
                },
            )
        else:
            await self._update_health(
                healthy=False,
                last_error="Failed to persist positions snapshot",
                extra_metrics={
                    "reason": reason,
                    "scope": "positions",
                    "command_id": outcome["command_id"],
                    "source": IB_COMMAND_SOURCE,
                },
            )
        return positions

    async def refresh_both(
        self, *, reason: str = "scheduled", fast_positions: bool = True
    ) -> Dict[str, Any]:
        """Refresh both account info and positions in sequence."""

        account_outcome = await self._execute_ib_command(
            "get_account_info",
            reason=reason,
            scope="account_info",
        )
        positions_outcome = await self._execute_ib_command(
            "get_positions",
            reason=reason,
            scope="positions",
            kwargs={"fast": fast_positions},
        )

        info: Optional[Dict[str, Any]] = None
        info_persisted = False
        info_missing = False
        if account_outcome["status"] == "success":
            info = account_outcome["result"]
            if info is not None:
                info_persisted = await write_account_info_snapshot(
                    info, IB_COMMAND_SOURCE
                )
            else:
                await self._update_health(
                    healthy=False,
                    last_error="Account info unavailable",
                    extra_metrics={
                        "reason": reason,
                        "scope": "account_info",
                        "command_id": account_outcome["command_id"],
                        "source": IB_COMMAND_SOURCE,
                    },
                )
                info_missing = True

        positions: Optional[Any] = None
        positions_persisted = False
        positions_missing = False
        if positions_outcome["status"] == "success":
            positions = positions_outcome["result"]
            if positions is not None:
                positions_persisted = await write_positions_snapshot(
                    positions, IB_COMMAND_SOURCE
                )
            else:
                await self._update_health(
                    healthy=False,
                    last_error="Positions unavailable",
                    extra_metrics={
                        "reason": reason,
                        "scope": "positions",
                        "command_id": positions_outcome["command_id"],
                        "source": IB_COMMAND_SOURCE,
                    },
                )
                positions_missing = True

        if (
            account_outcome["status"] != "success"
            or positions_outcome["status"] != "success"
            or info_missing
            or positions_missing
        ):
            return {"account_info": info, "positions": positions}

        now = now_local()
        if info_persisted:
            self._last_account_refresh = now
            self._log_snapshot_update(
                "account_info",
                reason=reason,
                refreshed_at=now,
                source=IB_COMMAND_SOURCE,
            )
        if positions_persisted:
            self._last_positions_refresh = now
            self._log_snapshot_update(
                "positions",
                reason=reason,
                refreshed_at=now,
                source=IB_COMMAND_SOURCE,
            )

        if info_persisted and positions_persisted:
            await self._update_health(
                healthy=True,
                extra_metrics={
                    "reason": reason,
                    "scope": "both",
                    "account_command_id": account_outcome["command_id"],
                    "positions_command_id": positions_outcome["command_id"],
                    "source": IB_COMMAND_SOURCE,
                },
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
                extra_metrics={
                    "reason": reason,
                    "scope": "both",
                    "account_command_id": account_outcome["command_id"],
                    "positions_command_id": positions_outcome["command_id"],
                    "source": IB_COMMAND_SOURCE,
                },
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
        self,
        scope: str,
        *,
        reason: str,
        refreshed_at: Optional[datetime],
        source: str = "ib-client",
    ) -> None:
        """Emit a structured log message after refreshing a snapshot."""

        refreshed_text = to_local_iso(refreshed_at) if refreshed_at else "unknown"
        next_refresh = self._next_refresh_display(refreshed_at)
        snapshot_service_logger.info(
            "{} snapshot updated (reason={}, source={}, refreshed_at={}, next_refresh={})",
            scope,
            reason,
            source,
            refreshed_text,
            next_refresh,
        )

    async def _execute_ib_command(
        self,
        method: str,
        *,
        reason: str,
        scope: str,
        args: Optional[list[Any]] = None,
        kwargs: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute an IB command and normalise the result for callers."""

        started = time.perf_counter()
        arguments = args or []
        keyword_arguments = dict(kwargs or {})
        try:
            record = await request_ib_command(
                method,
                args=arguments,
                kwargs=keyword_arguments,
                timeout=self._request_timeout_seconds,
            )
        except IBCommandTimeout as exc:
            duration = time.perf_counter() - started
            snapshot_service_logger.warning(
                "IB command {} timed out (command_id={}, duration={:.3f}s)",
                method,
                exc.command_id,
                duration,
            )
            await mark_all_snapshots_stale("ib-command-timeout")
            await self._update_health(
                healthy=False,
                last_error="timeout",
                extra_metrics={
                    "reason": reason,
                    "scope": scope,
                    "command_id": exc.command_id,
                    "source": IB_COMMAND_SOURCE,
                    "duration": f"{duration:.3f}",
                    "state": "timeout",
                },
            )
            return {
                "status": "timeout",
                "command_id": exc.command_id,
                "result": None,
                "error": "timeout",
                "duration": duration,
            }
        except IBCommandExecutionError as exc:
            duration = time.perf_counter() - started
            error_message = str(exc.payload.get("error") or str(exc))
            snapshot_service_logger.warning(
                "IB command {} failed (command_id={}, duration={:.3f}s, error={})",
                method,
                exc.command_id,
                duration,
                error_message,
            )
            await self._update_health(
                healthy=False,
                last_error=error_message,
                extra_metrics={
                    "reason": reason,
                    "scope": scope,
                    "command_id": exc.command_id,
                    "source": IB_COMMAND_SOURCE,
                    "duration": f"{duration:.3f}",
                    "state": "error",
                },
            )
            return {
                "status": "error",
                "command_id": exc.command_id,
                "result": None,
                "error": error_message,
                "duration": duration,
            }
        except Exception as exc:
            duration = time.perf_counter() - started
            snapshot_service_logger.warning(
                "IB command {} raised unexpected error (duration={:.3f}s): {}",
                method,
                duration,
                exc,
            )
            await self._update_health(
                healthy=False,
                last_error=str(exc),
                extra_metrics={
                    "reason": reason,
                    "scope": scope,
                    "source": IB_COMMAND_SOURCE,
                    "duration": f"{duration:.3f}",
                    "state": "exception",
                },
            )
            raise

        duration = time.perf_counter() - started
        command_id = str(record.get("command_id") or "")
        result_payload = record.get("result")
        snapshot_service_logger.debug(
            "IB command {} completed (command_id={}, duration={:.3f}s)",
            method,
            command_id,
            duration,
        )
        return {
            "status": "success",
            "command_id": command_id,
            "result": result_payload,
            "duration": duration,
        }


IB_COMMAND_SOURCE = "ib-command"
