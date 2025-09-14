import time
import asyncio
from typing import Dict, List

import core
from core import connection_manager, depth_subscriber, dom_config, DOMProcessor


class DOMService:
    """Service layer wrapping DOM processing and alerts."""

    def __init__(self) -> None:
        self.processor: DOMProcessor = core.dom_processor
        self._alerts: List[Dict[str, any]] = []
        self._next_alert_id: int = 1
        self._last_signal = {"long": False, "short": False}

    # --------------------------------------------------------------
    def _on_depth_update(self, data: Dict) -> None:
        """Internal callback from DepthSubscriber."""
        self.processor.on_depth_update(data)
        ts = time.time()
        signals = self.processor.last_signals
        if signals.get("long") and not self._last_signal.get("long"):
            asyncio.create_task(
                core.notification_manager.send_notification(
                    "DOM signal: LONG", event_type="dom_long"
                )
            )
        if signals.get("short") and not self._last_signal.get("short"):
            asyncio.create_task(
                core.notification_manager.send_notification(
                    "DOM signal: SHORT", event_type="dom_short"
                )
            )
        self._last_signal = signals
        for evt in self.processor.last_events:
            self._alerts.append({"id": self._next_alert_id, "event": evt, "timestamp": ts})
            self._next_alert_id += 1

    # --------------------------------------------------------------
    def start(self, symbol: str, num_rows: int = 5) -> bool:
        if not connection_manager.is_connected():
            return False
        return depth_subscriber.start_depth_stream(symbol, self._on_depth_update, num_rows=num_rows)

    # --------------------------------------------------------------
    def stop(self, symbol: str) -> bool:
        return depth_subscriber.stop_depth_stream(symbol)

    # --------------------------------------------------------------
    def get_metrics(self) -> Dict[str, float]:
        return {**self.processor.last_metrics, **self.processor.last_signals}

    # --------------------------------------------------------------
    def list_alerts(self) -> List[Dict[str, any]]:
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
