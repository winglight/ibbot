"""DOM depth adapter integrating the legacy subscriber with the event bus."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections import deque
from datetime import datetime
from threading import Lock
from typing import Any, Deque, Dict, Optional, TYPE_CHECKING
from zoneinfo import ZoneInfo

from loguru import logger

from ..data_source_manager import (
    DataSourceAdapter,
    DataSourceError,
    DataSubscriptionRequest,
)
from ..event_bus import EventEnvelope

if TYPE_CHECKING:  # pragma: no cover - import for type hints only
    from src.depth_subscriber import DepthSubscriber

LOCAL_TZ = ZoneInfo("Asia/Shanghai")
UTC_TZ = ZoneInfo("UTC")


def _to_local_iso(value: Optional[Any]) -> str:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = None
        if value is not None:
            text = str(value)
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                dt = datetime.fromisoformat(text)
            except Exception:  # pragma: no cover - defensive guard
                dt = None
        if dt is None:
            dt = datetime.now(UTC_TZ)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC_TZ)
    return dt.astimezone(LOCAL_TZ).isoformat()

__all__ = ["DomDepthAdapter"]


class DomDepthAdapter(DataSourceAdapter):
    """Bridge DOM depth updates into the unified data layer."""

    channel = "dom.depth"

    def __init__(
        self,
        depth_subscriber: "DepthSubscriber",
        event_bus,
        *,
        max_history: int = 512,
    ) -> None:
        super().__init__(event_bus)
        self._depth_subscriber = depth_subscriber
        self._max_history = max(1, int(max_history))
        self._snapshots: Dict[str, Dict[str, Any]] = {}
        self._history: Dict[str, Deque[Dict[str, Any]]] = {}
        self._callbacks: Dict[str, Any] = {}
        self._symbol_options: Dict[str, Dict[str, Any]] = {}
        self._lock = Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def subscribe(self, request: DataSubscriptionRequest):  # type: ignore[override]
        loop = asyncio.get_running_loop()
        self._loop = loop

        symbol = (request.symbol or "").strip().upper()
        if not symbol:
            raise DataSourceError("DOM subscription requires symbol")

        num_rows = int(request.options.get("num_rows", 5)) if request.options else 5
        num_rows = max(1, min(num_rows, 30))
        force_restart = bool(request.options.get("force_restart")) if request.options else False

        def _on_update(payload: Dict[str, Any], *, sym: str = symbol, rows: int = num_rows) -> None:
            self._handle_depth_update(sym, rows, payload)

        def _start() -> bool:
            if force_restart:
                try:
                    self._depth_subscriber.stop_depth_stream(symbol)
                except Exception as exc:  # pragma: no cover - defensive guard
                    logger.debug("Failed to stop depth stream during force_restart: {}", exc)

            start_fn = getattr(self._depth_subscriber, "start_depth_stream")
            kwargs = {"num_rows": num_rows}

            try:
                signature = inspect.signature(start_fn)
            except (TypeError, ValueError):
                signature = None

            if signature is None or "force_restart" in signature.parameters:
                kwargs["force_restart"] = force_restart
            elif force_restart:
                logger.debug(
                    "Depth subscriber does not accept force_restart; ignoring request",
                )

            return start_fn(symbol, _on_update, **kwargs)

        started = await asyncio.to_thread(_start)
        if not started:
            last_error, _status = self._depth_subscriber.get_last_error()
            raise DataSourceError(last_error or "Failed to start DOM depth stream")

        with self._lock:
            self._callbacks[symbol] = _on_update
            self._symbol_options[symbol] = {"num_rows": num_rows}

        async def _cancel() -> None:
            def _stop() -> bool:
                return self._depth_subscriber.stop_depth_stream(symbol)

            await asyncio.to_thread(_stop)
            with self._lock:
                self._callbacks.pop(symbol, None)
                self._symbol_options.pop(symbol, None)

        return _cancel

    async def snapshot(self, request: DataSubscriptionRequest):  # type: ignore[override]
        symbol = (request.symbol or "").strip().upper()
        if not symbol:
            raise DataSourceError("DOM snapshot requires symbol")
        with self._lock:
            snapshot = self._snapshots.get(symbol)
        return dict(snapshot) if snapshot else {}

    async def history(self, request: DataSubscriptionRequest):  # type: ignore[override]
        symbol = (request.symbol or "").strip().upper()
        if not symbol:
            raise DataSourceError("DOM history requires symbol")
        with self._lock:
            series = list(self._history.get(symbol, ()))
        return [dict(entry) for entry in series]

    def _handle_depth_update(self, symbol: str, num_rows: int, payload: Dict[str, Any]) -> None:
        bids = list(payload.get("bids", []) or [])
        asks = list(payload.get("asks", []) or [])
        ts_ns = payload.get("ts_ns")
        if not isinstance(ts_ns, int):
            ts_ns = time.time_ns()
        ts_iso = _to_local_iso(payload.get("timestamp"))

        snapshot = {
            "symbol": symbol,
            "bids": bids,
            "asks": asks,
            "ts_ns": ts_ns,
            "timestamp": ts_iso,
        }

        with self._lock:
            self._snapshots[symbol] = snapshot
            history = self._history.setdefault(symbol, deque(maxlen=self._max_history))
            history.append(snapshot)

        envelope = EventEnvelope(
            event_type="dom.depth",
            topic="dom.depth",
            symbol=symbol,
            source="ibkr",
            ts_ns=ts_ns,
            payload=dict(snapshot),
            meta={"num_rows": num_rows},
        )
        self._publish_async(envelope)

    def _publish_async(self, envelope: EventEnvelope) -> None:
        loop = self._loop
        if loop and loop.is_running():
            try:
                try:
                    running_loop = asyncio.get_running_loop()
                except RuntimeError:
                    running_loop = None

                if running_loop is loop:
                    loop.create_task(self.event_bus.publish(envelope))
                else:
                    def _dispatch() -> None:
                        loop.create_task(self.event_bus.publish(envelope))

                    loop.call_soon_threadsafe(_dispatch)
                return
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.exception("Failed to publish DOM depth event: {}", exc)
                return

        try:
            asyncio.run(self.event_bus.publish(envelope))
        except RuntimeError as exc:
            logger.debug("DOM depth publish fallback failed: %s", exc)
