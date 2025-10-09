"""Background service for refreshing account snapshots in Redis."""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, Mapping, Optional

from loguru import logger

from .account_snapshot_cache import (
    mark_all_snapshots_stale,
    write_account_info_snapshot,
    write_positions_snapshot,
)
from .utils import now_local, to_local_iso
from src.ibbot import update_service_health
from src.ibbot.redis import LegacyIBCommandUnavailableError, get_redis_client

if TYPE_CHECKING:  # pragma: no cover - optional dependency for type checking
    from services.account.ib_client import ServiceIBClient


def _read_interval_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return max(0, default)
    return max(0, value)


snapshot_service_logger = logger.bind(module="account_snapshot_service", request_id="-")

ACCOUNT_SUMMARY_TAGS: tuple[str, ...] = (
    "AccountCode",
    "AccountType",
    "TotalCashValue",
    "AvailableFunds",
    "BuyingPower",
    "NetLiquidation",
    "UnrealizedPnL",
    "RealizedPnL",
)
ACCOUNT_SUMMARY_TAG_STRING = ",".join(ACCOUNT_SUMMARY_TAGS)


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

        command_timeout_env = os.getenv("ACCOUNT_SNAPSHOT_COMMAND_TIMEOUT_SECONDS")
        refresh_timeout_env = os.getenv("ACCOUNT_SNAPSHOT_REFRESH_TIMEOUT_SECONDS")

        default_command_timeout = 30
        if command_timeout_env is not None:
            self._request_timeout_seconds = _read_interval_env(
                "ACCOUNT_SNAPSHOT_COMMAND_TIMEOUT_SECONDS",
                default_command_timeout,
            ) or default_command_timeout
        elif refresh_timeout_env is not None:
            self._request_timeout_seconds = _read_interval_env(
                "ACCOUNT_SNAPSHOT_REFRESH_TIMEOUT_SECONDS",
                default_command_timeout,
            ) or default_command_timeout
        else:
            self._request_timeout_seconds = default_command_timeout

        refresh_default = max(self._request_timeout_seconds * 2, 60)
        self._refresh_timeout_seconds = (
            _read_interval_env(
                "ACCOUNT_SNAPSHOT_REFRESH_TIMEOUT_SECONDS",
                refresh_default,
            )
            or refresh_default
        )

        if command_timeout_env is None and refresh_timeout_env is not None:
            self._request_timeout_seconds = self._refresh_timeout_seconds

        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._last_fill: Optional[datetime] = None
        self._last_account_refresh: Optional[datetime] = None
        self._last_positions_refresh: Optional[datetime] = None
        self._health_redis: Any = None
        self._health_last_error: str = ""
        self._ib_client: "ServiceIBClient" | None = None

    def set_ib_client(self, client: "ServiceIBClient" | None) -> None:
        """Inject the service-scoped IB client used for direct requests."""

        self._ib_client = client

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

    def _require_ib_client(self) -> "ServiceIBClient":
        client = self._ib_client
        if client is None:
            raise LegacyIBCommandUnavailableError("Account IB client unavailable")
        return client

    @staticmethod
    def _coerce_float(value: Any) -> float:
        if isinstance(value, Decimal):
            return float(value)
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _coerce_optional_float(value: Any) -> float | None:
        if isinstance(value, Decimal):
            return float(value)
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _timestamp_to_iso(value: Any) -> str:
        if isinstance(value, datetime):
            return to_local_iso(value)
        if isinstance(value, str) and value:
            return value
        return to_local_iso(now_local())

    async def _dispatch_command_via_client(
        self,
        method: str,
        *,
        args: list[Any],
        kwargs: dict[str, Any],
    ) -> Dict[str, Any]:
        client = self._require_ib_client()
        _ = args  # arguments are retained for parity with legacy pipeline
        timeout = float(kwargs.pop("timeout", self._request_timeout_seconds))

        if method == "get_account_info":
            result = await self._fetch_account_info(client, timeout=timeout)
        elif method == "get_positions":
            result = await self._fetch_positions(client, timeout=timeout)
        else:
            raise LegacyIBCommandUnavailableError(
                f"Unsupported ib_command method '{method}' for account snapshots"
            )

        command_id = kwargs.get("command_id")
        if not command_id:
            command_id = f"local:{method}:{int(time.time() * 1000)}"
        return {
            "status": "success",
            "command_id": command_id,
            "result": result,
            "source": IB_COMMAND_SOURCE,
        }

    async def _fetch_account_info(
        self,
        client: "ServiceIBClient",
        *,
        timeout: float,
    ) -> Dict[str, Any]:
        summary = await client.fetch_account_summary(
            ACCOUNT_SUMMARY_TAG_STRING,
            timeout=timeout,
        )
        accounts = summary.get("accounts") or {}
        currencies = summary.get("currencies") or {}

        account_id: str | None = None
        account_values: Mapping[str, Any] = {}
        for candidate, values in accounts.items():
            account_id = candidate
            account_values = values or {}
            break

        info: Dict[str, Any] = {}
        if account_id:
            info["account_id"] = account_id

        account_type = account_values.get("AccountType")
        if account_type is not None:
            info["account_type"] = str(account_type)

        info["total_cash"] = self._coerce_float(account_values.get("TotalCashValue"))
        info["available_funds"] = self._coerce_float(account_values.get("AvailableFunds"))
        info["buying_power"] = self._coerce_float(account_values.get("BuyingPower"))
        info["net_liquidation"] = self._coerce_float(account_values.get("NetLiquidation"))
        info["unrealized_pnl"] = (
            self._coerce_optional_float(account_values.get("UnrealizedPnL")) or 0.0
        )
        info["realized_pnl"] = (
            self._coerce_optional_float(account_values.get("RealizedPnL")) or 0.0
        )

        base_currency = None
        currency_map = currencies.get(account_id or "")
        if isinstance(currency_map, Mapping):
            base_currency = currency_map.get("NetLiquidation") or currency_map.get(
                "TotalCashValue"
            )
        if isinstance(base_currency, str) and base_currency:
            info["base_currency"] = base_currency

        received_at = summary.get("received_at")
        timestamp = self._timestamp_to_iso(received_at)
        info["received_at"] = timestamp
        info.setdefault("updated_at", timestamp)
        return info

    async def _fetch_positions(
        self,
        client: "ServiceIBClient",
        *,
        timeout: float,
    ) -> list[Dict[str, Any]]:
        events = await client.fetch_positions(timeout=timeout)
        payload: list[Dict[str, Any]] = []
        for event in events:
            payload.append(self._normalise_position_event(event))
        return payload

    def _normalise_position_event(self, event: Any) -> Dict[str, Any]:
        if hasattr(event, "model_dump"):
            data = event.model_dump()
        elif isinstance(event, Mapping):
            data = dict(event)
        else:
            data = {}

        normalised: Dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(value, Decimal):
                normalised[key] = float(value)
            elif isinstance(value, datetime):
                normalised[key] = self._timestamp_to_iso(value)
            else:
                normalised[key] = value
        normalised.setdefault("timestamp", self._timestamp_to_iso(data.get("timestamp")))
        return normalised

    # ------------------------------------------------------------------
    # Lifecycle management

    async def start(self) -> None:
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

        try:
            await asyncio.wait_for(
                self.refresh_both(reason="startup", fast_positions=True),
                timeout=self._refresh_timeout_seconds,
            )
        except asyncio.TimeoutError:
            snapshot_service_logger.warning(
                "Startup account snapshot refresh timed out"
            )
            await self._update_health(
                healthy=False,
                last_error="Startup refresh timed out",
                extra_metrics={"reason": "startup", "state": "timeout"},
            )
        except Exception as exc:
            snapshot_service_logger.warning(
                "Startup account snapshot refresh failed: {}", exc
            )
            await self._update_health(
                healthy=False,
                last_error=str(exc),
                extra_metrics={"reason": "startup", "state": "error"},
            )

        self._task = loop.create_task(self._run_loop())
        snapshot_service_logger.info(
            "Account snapshot service started (refresh_interval={}s, poll={}s, command_timeout={}s, refresh_timeout={}s)",
            self.refresh_interval_seconds,
            self._poll_interval_seconds,
            self._request_timeout_seconds,
            self._refresh_timeout_seconds,
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
                    timeout=self._refresh_timeout_seconds,
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
        keyword_arguments = dict(kwargs or {})
        try:
            record = await self._dispatch_command_via_client(
                method,
                args=list(args or []),
                kwargs=keyword_arguments,
            )
        except LegacyIBCommandUnavailableError as exc:
            duration = time.perf_counter() - started
            message = str(exc)
            snapshot_service_logger.warning(
                "Account IB client unavailable for method %s (duration=%.3fs)",
                method,
                duration,
            )
            await mark_all_snapshots_stale("ib-command-removed")
            await self._update_health(
                healthy=False,
                last_error=message,
                extra_metrics={
                    "reason": reason,
                    "scope": scope,
                    "command_id": None,
                    "source": IB_COMMAND_SOURCE,
                    "duration": f"{duration:.3f}",
                    "state": "unavailable",
                },
            )
            return {
                "status": "unavailable",
                "command_id": None,
                "result": None,
                "error": message,
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
        record.setdefault("duration", duration)
        record.setdefault("command_id", f"local:{method}")
        record.setdefault("source", IB_COMMAND_SOURCE)
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


IB_COMMAND_SOURCE = "service:account:ib-client"
