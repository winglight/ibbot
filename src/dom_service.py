import time
import asyncio
from typing import Dict, List, Optional, Any

import core
from core import connection_manager, depth_subscriber, dom_config, DOMProcessor
from .notifier import Notifier


class DOMService:
    """Service layer wrapping DOM processing and alerts."""

    def __init__(self) -> None:
        self.processor: DOMProcessor = core.dom_processor
        self.notifier = Notifier(core.notification_manager)
        self._alerts: List[Dict[str, Any]] = []
        self._next_alert_id: int = 1
        self._last_signal = {"long": False, "short": False}
        self._max_alerts = 100
        self._retention_sec = 300
        self._cleanup_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Store last raw depth update for monitoring
        self._last_depth: Dict[str, Any] = {}

    # --------------------------------------------------------------
    def _on_depth_update(self, data: Dict) -> None:
        """Internal callback from DepthSubscriber."""
        # Keep raw depth snapshot for external monitoring
        self._last_depth = data
        self.processor.on_depth_update(data)
        ts = time.time()
        signals = self.processor.last_signals
        if signals.get("long") and not self._last_signal.get("long"):
            if self._loop:
                asyncio.run_coroutine_threadsafe(
                    self.notifier.send("DOM signal: LONG", event_type="dom_long"),
                    self._loop,
                )
        if signals.get("short") and not self._last_signal.get("short"):
            if self._loop:
                asyncio.run_coroutine_threadsafe(
                    self.notifier.send("DOM signal: SHORT", event_type="dom_short"),
                    self._loop,
                )
        self._last_signal = signals
        for evt in self.processor.last_events:
            self._alerts.append({"id": self._next_alert_id, "event": evt, "timestamp": ts})
            self._next_alert_id += 1
        self._perform_cleanup()

    # --------------------------------------------------------------
    def _perform_cleanup(self) -> None:
        cutoff = time.time() - self._retention_sec
        self._alerts = [a for a in self._alerts if a["timestamp"] >= cutoff]
        if len(self._alerts) > self._max_alerts:
            self._alerts = self._alerts[-self._max_alerts :]

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            self._perform_cleanup()

    # --------------------------------------------------------------
    def start(self, symbol: str, num_rows: int = 5) -> bool:
        if not connection_manager.is_connected():
            return False
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = asyncio.new_event_loop()
        if self._cleanup_task is None or self._cleanup_task.done():
            try:
                self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            except RuntimeError:
                self._cleanup_task = None
        return depth_subscriber.start_depth_stream(
            symbol, self._on_depth_update, num_rows=num_rows
        )

    # --------------------------------------------------------------
    def stop(self, symbol: str) -> bool:
        return depth_subscriber.stop_depth_stream(symbol)

    # --------------------------------------------------------------
    def get_metrics(self) -> Dict[str, float]:
        return {**self.processor.last_metrics, **self.processor.last_signals}

    # --------------------------------------------------------------
    def get_last_depth(self) -> Dict[str, Any]:
        """Return last raw depth data received from IB."""
        return dict(self._last_depth)

    # --------------------------------------------------------------
    def list_alerts(self) -> List[Dict[str, Any]]:
        return list(self._alerts)

    # --------------------------------------------------------------
    def ack_alert(self, alert_id: int) -> bool:
        for i, a in enumerate(self._alerts):
            if a.get("id") == alert_id:
                self._alerts.pop(i)
                return True
        return False

    # --------------------------------------------------------------
    def update_config(self, window: int, max_levels: int) -> None:
        dom_config.window = window
        dom_config.max_levels = max_levels
        self.processor = DOMProcessor(window=window, max_levels=max_levels)
        core.dom_processor = self.processor

    # --------------------------------------------------------------
    def get_events(self) -> List[str]:
        return list(self.processor.last_events)


# Global service instance
_dom_service = DOMService()

def get_dom_service() -> DOMService:
    return _dom_service
