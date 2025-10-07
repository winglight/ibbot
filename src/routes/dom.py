from typing import Any, Dict, List, Optional

import asyncio
import json
import time
from datetime import datetime

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, Field

from .auth import get_current_user
from src.dom_service import DOMService, get_dom_service
from src.dom_dependencies import dom_config
from loguru import logger

router = APIRouter()


def get_dom_service_client() -> DOMService:
    """Return the active DOM service instance.

    Legacy route tests monkeypatch this helper to substitute a lightweight
    in-memory service.  The module keeps referencing ``dom_service`` for
    brevity, so we expose a proxy object that delegates attribute access to the
    current client returned by this function.
    """

    return get_dom_service()


class _DomServiceProxy:
    def __getattr__(self, item):
        return getattr(get_dom_service_client(), item)


dom_service = _DomServiceProxy()

DOM_STOP_TIMEOUT = 2.5


def _ensure_not_read_only(request: Request) -> None:
    if getattr(request.state, "is_read_only", False):
        raise HTTPException(status_code=403, detail="Read-only access")


class StartRequest(BaseModel):
    symbol: str
    num_rows: int = Field(default=5, ge=1, le=30)
    force_restart: bool = False


@router.post("/api/dom/start")
async def dom_start(
    req: StartRequest,
    request: Request,
    user: Optional[str] = Depends(get_current_user),
):
    _ensure_not_read_only(request)
    symbol = (req.symbol or "").strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="Symbol is required")

    force_restart = bool(req.force_restart)

    if dom_service.is_stream_active(symbol) and not force_restart:
        return {"success": True, "pending": False, "already_running": True}

    if dom_service.is_start_in_progress(symbol) and not force_restart:
        return {"success": True, "pending": True, "starting": True}

    try:
        start_task = dom_service.request_stream_start(
            symbol, num_rows=req.num_rows, force_restart=force_restart
        )
    except RuntimeError:
        ok = await dom_service.start_stream(symbol, num_rows=req.num_rows, force_restart=force_restart)
        if not ok:
            error_message, status_code = dom_service.get_last_start_error()
            detail = error_message or "Failed to start depth stream"
            status = (
                status_code
                if isinstance(status_code, int) and 400 <= status_code < 600
                else 500
            )
            raise HTTPException(status_code=status, detail=detail)
        return {"success": True, "pending": False}

    try:
        ok = await asyncio.wait_for(asyncio.shield(start_task), timeout=1.2)
    except asyncio.TimeoutError:
        return {"success": True, "pending": True, "starting": True}
    except Exception as exc:
        logger.error("DOM start task failed", symbol=symbol, error=str(exc))
        raise HTTPException(status_code=500, detail="Failed to start depth stream") from exc

    if not ok:
        error_message, status_code = dom_service.get_last_start_error()
        detail = error_message or "Failed to start depth stream"
        status = status_code if isinstance(status_code, int) and 400 <= status_code < 600 else 500
        raise HTTPException(status_code=status, detail=detail)
    return {"success": True, "pending": False, "already_running": False}


class StopRequest(BaseModel):
    symbol: str


@router.post("/api/dom/stop")
async def dom_stop(
    req: StopRequest,
    request: Request,
    user: Optional[str] = Depends(get_current_user),
):
    _ensure_not_read_only(request)
    symbol = (req.symbol or "").strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="Symbol is required")
    stop_task = asyncio.create_task(dom_service.stop_stream(symbol))
    try:
        ok = await asyncio.wait_for(asyncio.shield(stop_task), timeout=DOM_STOP_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning(
            "DOM stop request timed out; continuing in background",
            symbol=symbol,
        )

        def _log_result(fut: asyncio.Future) -> None:
            try:
                result = fut.result()
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.warning(
                    "Background DOM stop task failed",
                    symbol=symbol,
                    error=str(exc),
                )
            else:
                if result:
                    logger.info(
                        "DOM stop completed asynchronously",
                        symbol=symbol,
                    )
                else:
                    logger.warning(
                        "DOM stop background task reported no active stream",
                        symbol=symbol,
                    )

        stop_task.add_done_callback(_log_result)
        return {"success": True, "pending": True}
    except Exception as exc:
        stop_task.cancel()
        logger.error("DOM stop task failed", symbol=symbol, error=str(exc))
        raise HTTPException(status_code=500, detail="Failed to stop depth stream") from exc

    if not ok:
        raise HTTPException(status_code=400, detail="No active depth stream for symbol")
    return {"success": True, "pending": False}


class DOMConfigModel(BaseModel):
    window: int = dom_config.window
    max_levels: int = dom_config.max_levels
    trend_thresholds: List[float] = Field(
        default_factory=lambda: list(getattr(dom_config, "trend_thresholds", []))
    )


@router.get("/api/config/dom")
async def get_dom_config(user: Optional[str] = Depends(get_current_user)):
    return dom_service.get_config()


@router.put("/api/config/dom")
async def update_dom_config(
    cfg: DOMConfigModel,
    request: Request,
    user: Optional[str] = Depends(get_current_user),
):
    _ensure_not_read_only(request)
    dom_service.update_config(cfg.window, cfg.max_levels, cfg.trend_thresholds)
    return {
        "success": True,
        "config": dom_service.get_config(),
    }


@router.get("/api/dom/data")
async def get_dom_data(
    symbol: Optional[str] = None,
    user: Optional[str] = Depends(get_current_user),
):
    return {
        "success": True,
        "metrics": dom_service.get_metrics(symbol),
        "depth": dom_service.get_last_depth(symbol),
    }


@router.get("/api/alerts")
async def get_alerts(user: Optional[str] = Depends(get_current_user)):
    return {"success": True, "alerts": dom_service.list_alerts()}


@router.post("/api/alerts/{alert_id}/ack")
async def ack_alert(
    alert_id: int,
    request: Request,
    user: Optional[str] = Depends(get_current_user),
):
    _ensure_not_read_only(request)
    ok = dom_service.ack_alert(alert_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Alert not found")
    return {"success": True}


@router.websocket("/ws/dom/{symbol}")
async def dom_ws(websocket: WebSocket, symbol: str):
    await websocket.accept()

    loop = asyncio.get_running_loop()
    symbol_arg = symbol
    symbol_key = (symbol_arg or "").upper()
    update_queue: asyncio.Queue[tuple[Optional[Dict[str, Any]], Any]] = asyncio.Queue(maxsize=1)

    def _resolve_timestamp(metrics: Optional[Dict[str, Any]]) -> int:
        now_ms = int(time.time() * 1000)
        if not isinstance(metrics, dict):
            return now_ms

        def _coerce(value: Any) -> Optional[int]:
            if isinstance(value, (int, float)):
                if value > 1e15:
                    return int(value / 1_000_000)
                if value > 1e12:
                    return int(value / 1_000)
                if value > 1e10:
                    return int(value)
                return int(value * 1000)
            if isinstance(value, str):
                text = value.strip()
                if not text:
                    return None
                try:
                    numeric = float(text)
                except ValueError:
                    try:
                        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                    except ValueError:
                        return None
                    else:
                        return int(parsed.timestamp() * 1000)
                else:
                    return _coerce(numeric)
            return None

        for key in ("timestamp_ms", "timestamp", "timestamp_ns", "ts_ns", "time"):
            if key not in metrics:
                continue
            value = _coerce(metrics.get(key))
            if value is not None:
                return value

        return now_ms

    async def _send_update(
        *, metrics: Optional[Dict[str, Any]], events: Any, error: Optional[str] = None
    ) -> None:
        timestamp_ms = _resolve_timestamp(metrics)
        payload_body: Dict[str, Any] = {"metrics": metrics, "events": events}
        if error is not None:
            payload_body["error"] = error

        payload_data = payload_body.copy()
        payload_data["timestamp"] = timestamp_ms
        payload_data["timestamp_ms"] = timestamp_ms
        message: Dict[str, Any] = {
            "type": "dom_update",
            "symbol": symbol,
            "timestamp": timestamp_ms,
            "timestamp_ms": timestamp_ms,
            **payload_body,
        }
        message["payload"] = payload_data

        await websocket.send_json(message)

    def _make_signature(value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, dict) and not value:
            return None
        try:
            return json.dumps(value, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return repr(value)

    def _get_events() -> Any:
        provider = dom_service.get_events
        try:
            return provider(symbol_arg)
        except TypeError:
            return provider()

    async def _enqueue_update(metrics: Optional[Dict[str, Any]], events: Any) -> None:
        while True:
            try:
                update_queue.get_nowait()
                update_queue.task_done()
            except asyncio.QueueEmpty:
                break
        await update_queue.put((metrics, events))

    def _listener(event_symbol: str, metrics: Dict[str, Any], _snapshot: Dict[str, Any]) -> None:
        if symbol_key and event_symbol.upper() != symbol_key:
            return

        metrics_copy = dict(metrics) if metrics else None
        events = _get_events()

        try:
            asyncio.run_coroutine_threadsafe(
                _enqueue_update(metrics_copy, events),
                loop,
            )
        except RuntimeError:
            # Event loop likely closed; ignore and let websocket exit.
            pass

    listener_registered = False

    try:
        dom_service.add_listener(_listener)
        listener_registered = True

        last_metrics_signature: Optional[str] = None
        last_events_signature: Optional[str] = None
        last_error: Optional[str] = None

        initial_metrics = dom_service.get_metrics(symbol_arg)
        initial_events = _get_events()
        await _enqueue_update(initial_metrics, initial_events)

        while True:
            try:
                metrics, events = await asyncio.wait_for(update_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                metrics = dom_service.get_metrics(symbol_arg)
                events = _get_events()
            else:
                update_queue.task_done()

            metrics_signature = _make_signature(metrics)
            events_signature = _make_signature(events)

            if metrics_signature and metrics_signature != last_metrics_signature:
                await _send_update(metrics=metrics, events=events)
                last_metrics_signature = metrics_signature
                last_events_signature = events_signature
                last_error = None
                continue

            if events_signature != last_events_signature:
                await _send_update(
                    metrics=metrics if metrics_signature else None,
                    events=events,
                )
                last_events_signature = events_signature
                if not metrics_signature:
                    last_metrics_signature = None
                last_error = None
                continue

            if not metrics_signature:
                error_message = "DOM metrics unavailable"
            else:
                error_message = "DOM metrics stale: no new data received"

            if last_error != error_message:
                await _send_update(metrics=None, events=events, error=error_message)
                last_error = error_message
                last_events_signature = events_signature
                if not metrics_signature:
                    last_metrics_signature = None
    except WebSocketDisconnect:
        return
    finally:
        if listener_registered:
            dom_service.remove_listener(_listener)


@router.get("/api/dom/history/{symbol}")
async def get_dom_history(
    symbol: str,
    limit: int = Query(500, ge=0),
    start: Optional[str] = None,
    end: Optional[str] = None,
    include_snapshots: bool = Query(True),
    user: Optional[str] = Depends(get_current_user),
):
    history = dom_service.get_history(
        symbol,
        limit=None if limit == 0 else limit,
        start_time=start,
        end_time=end,
        include_snapshots=include_snapshots,
    )
    return {"success": True, "symbol": symbol, **history}


@router.get("/api/dom/stats/{symbol}")
async def get_dom_stats(
    symbol: str,
    lookback_seconds: Optional[int] = Query(None, ge=60),
    start: Optional[str] = None,
    end: Optional[str] = None,
    max_points: Optional[int] = Query(None, ge=10, le=5000),
    user: Optional[str] = Depends(get_current_user),
):
    response = await dom_service.get_stats_async(
        symbol,
        lookback_seconds=lookback_seconds,
        start_time=start,
        end_time=end,
        max_points=max_points,
    )
    response["success"] = True
    return response


