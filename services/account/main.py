from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import JSONResponse
from loguru import logger

from src.account_snapshot_cache import (
    read_account_info_snapshot,
    read_positions_snapshot,
)
from src.account_snapshot_service import AccountSnapshotService
from src.ibbot import update_service_health
from src.ibbot.redis import get_redis_client, request_ib_command
from src.utils import now_local, parse_local_datetime, to_local_iso


ACCOUNT_SERVICE_NAME = "account-service"
DEFAULT_HEALTH_INTERVAL = 30.0


@dataclass
class AccountServiceState:
    """Container storing runtime state for the service."""

    redis: Any | None = None
    health_task: asyncio.Task | None = None
    watchdog_task: asyncio.Task | None = None
    watchdog_stop_event: asyncio.Event | None = None
    snapshot_service: AccountSnapshotService | None = None
    last_refresh_request: datetime | None = None
    health_interval: float = DEFAULT_HEALTH_INTERVAL
    stale: bool = False
    last_refresh_error: Optional[str] = None
    snapshot_updated_at: Optional[str] = None

    def update_snapshot_metadata(
        self, *, info: Optional[Dict[str, Any]], positions: Optional[Dict[str, Any]]
    ) -> None:
        timestamps = []
        if info:
            updated_at = info.get("updated_at")
            if updated_at:
                timestamps.append(updated_at)
        if positions:
            updated_at = positions.get("updated_at")
            if updated_at:
                timestamps.append(updated_at)
        self.snapshot_updated_at = max(timestamps) if timestamps else None

    def as_metrics(
        self, *, info: Optional[Dict[str, Any]], positions: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        self.update_snapshot_metadata(info=info, positions=positions)
        metrics: Dict[str, Any] = {
            "last_refresh_requested": to_local_iso(self.last_refresh_request) or "",
            "account_snapshot_updated_at": (info or {}).get("updated_at", ""),
            "positions_snapshot_updated_at": (positions or {}).get("updated_at", ""),
            "snapshot_updated_at": self.snapshot_updated_at or "",
            "stale": self.stale,
            "last_refresh_error": self.last_refresh_error or "",
        }
        return metrics


app = FastAPI(title="Account Service")
_state = AccountServiceState()


def _read_health_interval() -> float:
    raw = os.getenv("ACCOUNT_SERVICE_HEALTH_INTERVAL_SECONDS", "")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_HEALTH_INTERVAL
    return max(0.0, value)


async def _collect_snapshots() -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    info_snapshot = await read_account_info_snapshot()
    positions_snapshot = await read_positions_snapshot()
    return info_snapshot, positions_snapshot


def _evaluate_snapshot_freshness(
    info_snapshot: Optional[Dict[str, Any]],
    positions_snapshot: Optional[Dict[str, Any]],
    *,
    refresh_interval: float,
    now: Optional[datetime] = None,
) -> Tuple[bool, Optional[str]]:
    if refresh_interval <= 0:
        return False, None

    reference = now or now_local()
    threshold = timedelta(seconds=refresh_interval * 2)
    stale_reasons: list[str] = []

    for label, snapshot in (
        ("account:info", info_snapshot),
        ("account:positions", positions_snapshot),
    ):
        if not snapshot:
            stale_reasons.append(f"{label} missing")
            continue
        updated_at = parse_local_datetime(snapshot.get("updated_at"))
        if updated_at is None:
            stale_reasons.append(f"{label} missing timestamp")
            continue
        if reference - updated_at > threshold:
            stale_reasons.append(f"{label} stale")

    if stale_reasons:
        return True, ", ".join(stale_reasons)
    return False, None


async def _health_publisher_loop() -> None:
    interval = _state.health_interval
    if interval <= 0:
        logger.info("Account service health publisher disabled (interval=%s)", interval)
        return

    logger.info("Starting account service health publisher (interval=%.1fs)", interval)

    while True:
        try:
            info_snapshot, positions_snapshot = await _collect_snapshots()
            metrics = _state.as_metrics(info=info_snapshot, positions=positions_snapshot)
            healthy = any((info_snapshot, positions_snapshot)) and not _state.stale
            redis_client = _state.redis
            if redis_client is not None:
                await update_service_health(
                    redis_client,
                    ACCOUNT_SERVICE_NAME,
                    healthy=healthy,
                    last_error=_state.last_refresh_error,
                    metrics=metrics,
                    logger=logger,
                )
        except asyncio.CancelledError:  # pragma: no cover - cooperative cancellation
            raise
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.warning("Failed to publish account service health: %s", exc)

        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:  # pragma: no cover - cooperative cancellation
            raise


async def _snapshot_watchdog_loop() -> None:
    service = _state.snapshot_service
    stop_event = _state.watchdog_stop_event
    if service is None or stop_event is None:
        logger.info("Account snapshot watchdog disabled (service unavailable)")
        return

    refresh_interval = float(service.refresh_interval_seconds)
    if refresh_interval <= 0:
        logger.info("Account snapshot watchdog disabled (refresh_interval=%s)", refresh_interval)
        return

    check_interval = max(5.0, min(refresh_interval, 60.0))
    logger.info(
        "Starting account snapshot watchdog (interval=%.1fs, threshold=%.1fs)",
        check_interval,
        refresh_interval * 2,
    )

    while not stop_event.is_set():
        try:
            info_snapshot, positions_snapshot = await _collect_snapshots()
        except asyncio.CancelledError:  # pragma: no cover - cooperative cancellation
            raise
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.warning("Watchdog failed to collect account snapshots: %s", exc)
            info_snapshot = None
            positions_snapshot = None

        stale, reason = _evaluate_snapshot_freshness(
            info_snapshot,
            positions_snapshot,
            refresh_interval=refresh_interval,
        )

        redis_client = _state.redis
        previously_stale = _state.stale

        if stale:
            _state.stale = True
            _state.last_refresh_error = reason or "snapshots stale"
            try:
                service.request_immediate_refresh(reason="watchdog-stale")
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.warning("Watchdog failed to request immediate refresh: %s", exc)
            if redis_client is not None:
                await update_service_health(
                    redis_client,
                    ACCOUNT_SERVICE_NAME,
                    healthy=False,
                    last_error=reason,
                    metrics=_state.as_metrics(
                        info=info_snapshot, positions=positions_snapshot
                    ),
                    logger=logger,
                )
        else:
            _state.stale = False
            _state.last_refresh_error = None
            if previously_stale and redis_client is not None:
                await update_service_health(
                    redis_client,
                    ACCOUNT_SERVICE_NAME,
                    healthy=True,
                    last_error="",
                    metrics=_state.as_metrics(
                        info=info_snapshot, positions=positions_snapshot
                    ),
                    logger=logger,
                )

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=check_interval)
            break
        except asyncio.TimeoutError:
            continue


@app.on_event("startup")
async def on_startup() -> None:
    _state.health_interval = _read_health_interval()
    _state.watchdog_stop_event = asyncio.Event()
    _state.stale = False
    _state.last_refresh_error = None
    _state.snapshot_updated_at = None
    try:
        _state.redis = await get_redis_client()
    except Exception as exc:  # pragma: no cover - redis optional for local tests
        logger.warning("Failed to acquire Redis client: %s", exc)
        _state.redis = None

    _state.snapshot_service = AccountSnapshotService()
    service = _state.snapshot_service

    if service is not None:
        await service.start()

    if _state.health_task is not None and not _state.health_task.done():
        _state.health_task.cancel()

    _state.health_task = asyncio.create_task(_health_publisher_loop())
    if _state.watchdog_task is not None and not _state.watchdog_task.done():
        _state.watchdog_task.cancel()

    _state.watchdog_task = asyncio.create_task(_snapshot_watchdog_loop())
    logger.info("Account service startup complete")


@app.on_event("shutdown")
async def on_shutdown() -> None:
    if _state.watchdog_stop_event is not None:
        _state.watchdog_stop_event.set()

    task = _state.health_task
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:  # pragma: no cover - expected cancellation
            pass
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.warning("Account service health task shutdown error: %s", exc)
    _state.health_task = None

    watchdog_task = _state.watchdog_task
    if watchdog_task is not None:
        watchdog_task.cancel()
        try:
            await watchdog_task
        except asyncio.CancelledError:  # pragma: no cover - expected cancellation
            pass
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.warning("Account service watchdog shutdown error: %s", exc)
    _state.watchdog_task = None

    service = _state.snapshot_service
    if service is not None:
        try:
            await service.stop()
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.warning("Account snapshot service shutdown error: %s", exc)
    _state.snapshot_service = None
    _state.watchdog_stop_event = None
    logger.info("Account service shutdown complete")


def _snapshot_response(snapshot: Dict[str, Any], *, data_key: str) -> Dict[str, Any]:
    return {
        "success": True,
        data_key: snapshot.get("data"),
        "from_cache": True,
        "cache_updated_at": snapshot.get("updated_at"),
        "cache_source": snapshot.get("source"),
        "stale": snapshot.get("stale", False),
        "stale_reason": snapshot.get("stale_reason"),
        "stale_at": snapshot.get("stale_at"),
    }


async def _ensure_snapshot(snapshot: Optional[Dict[str, Any]], *, kind: str) -> Dict[str, Any]:
    if not snapshot or "data" not in snapshot:
        message = f"{kind} snapshot unavailable"
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=message)
    return snapshot


@app.get("/account/info")
async def get_account_info() -> Dict[str, Any]:
    snapshot = await _ensure_snapshot(
        await read_account_info_snapshot(),
        kind="Account info",
    )
    return _snapshot_response(snapshot, data_key="account_info")


@app.get("/account/positions")
async def get_account_positions() -> Dict[str, Any]:
    snapshot = await _ensure_snapshot(
        await read_positions_snapshot(),
        kind="Positions",
    )
    return _snapshot_response(snapshot, data_key="positions")


@app.get("/account/orders")
async def get_account_orders() -> Dict[str, Any]:
    return {"success": True, "orders": [], "from_cache": True}


@app.get("/account/daily-summary")
async def get_daily_summary() -> Dict[str, Any]:
    return {"success": True, "daily_summary": [], "from_cache": True}


@app.post("/account/refresh")
async def post_refresh_request() -> JSONResponse:
    _state.last_refresh_request = now_local()
    requested_at = to_local_iso(_state.last_refresh_request)
    redis_client = _state.redis
    triggered = False
    error: Optional[str] = None

    if redis_client is not None:
        try:
            await request_ib_command("refresh_account", redis=redis_client, timeout=5)
            triggered = True
        except Exception as exc:  # pragma: no cover - optional command may fail
            error = str(exc)
            logger.warning("Failed to request account refresh: %s", exc)
    else:
        error = "Redis client unavailable"

    payload: Dict[str, Any] = {
        "success": triggered and error is None,
        "requested_at": requested_at,
        "triggered": triggered,
        "from_cache": False,
    }
    if error:
        payload["error"] = error
    return JSONResponse(status_code=status.HTTP_200_OK, content=payload)


__all__ = ["app"]
