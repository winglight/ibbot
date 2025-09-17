import time
import asyncio
import inspect
from typing import Dict, List, Optional, Any, Callable, DefaultDict
from collections import defaultdict

from loguru import logger

from src.connection_manager import connection_manager
from src.dom_cache import DomCacheManager
from src.dom_dependencies import (
    dom_config,
    depth_subscriber,
    get_dom_processor,
    set_dom_processor,
)
from src.database import get_db_manager
from src.dom_processor import DOMProcessor

DEFAULT_TREND_THRESHOLDS = [0.5, 0.7, 0.85, 0.95]


class DOMService:
    """Service layer wrapping DOM processing and alerts."""

    def __init__(self) -> None:
        self.processor: DOMProcessor = get_dom_processor()
        self._alerts: List[Dict[str, Any]] = []
        self._next_alert_id: int = 1
        self._max_alerts = 100
        self._retention_sec = 300
        self._cleanup_task: Optional[asyncio.Task] = None
        # Store last raw depth update for monitoring
        self._last_depth: Dict[str, Any] = {}
        self._last_depth_by_symbol: Dict[str, Dict[str, Any]] = {}
        self._last_metrics_by_symbol: Dict[str, Dict[str, Any]] = {}
        self._active_symbol: Optional[str] = None
        self._cache = DomCacheManager()
        self.db_manager = get_db_manager()
        self.trend_thresholds: List[float] = list(
            getattr(dom_config, "trend_thresholds", DEFAULT_TREND_THRESHOLDS)
        ) or list(DEFAULT_TREND_THRESHOLDS)
        self._listeners: DefaultDict[str, List[Callable]] = defaultdict(list)

        persisted_config = self._load_persisted_config()
        if persisted_config:
            self._apply_config(
                window=persisted_config.get("window", dom_config.window),
                max_levels=persisted_config.get("max_levels", dom_config.max_levels),
                trend_thresholds=persisted_config.get("trend_thresholds"),
                persist=False,
            )

    # --------------------------------------------------------------
    def _on_depth_update(self, symbol: str, data: Dict) -> None:
        """Internal callback from DepthSubscriber."""
        # Keep raw depth snapshot for external monitoring
        symbol_key = (symbol or "").upper()
        snapshot = dict(data)
        snapshot.setdefault("symbol", symbol_key)
        ts_ns = snapshot.get("ts_ns")
        if not isinstance(ts_ns, int):
            try:
                ts_ns = int(ts_ns)
            except (TypeError, ValueError):
                ts_ns = None
        if ts_ns is None or ts_ns < 1_000_000_000_000:
            ts_ns = time.time_ns()
        snapshot["ts_ns"] = ts_ns
        self._last_depth = snapshot
        if symbol_key:
            self._last_depth_by_symbol[symbol_key] = snapshot

        self.processor.on_depth_update(snapshot)
        metrics = dict(self.processor.last_metrics)
        if symbol_key:
            metrics["symbol"] = symbol_key
            self._last_metrics_by_symbol[symbol_key] = metrics
        timestamp = metrics.get("timestamp") or (ts_ns / 1_000_000.0)
        snapshot.setdefault("timestamp", timestamp)
        self._cache.append(symbol_key, snapshot, metrics)

        ts = time.time()
        for evt in self.processor.last_events:
            self._alerts.append({"id": self._next_alert_id, "event": evt, "timestamp": ts})
            self._next_alert_id += 1
        self._perform_cleanup()
        self._dispatch_event("metrics", symbol_key, metrics, snapshot)

    def _dispatch_event(self, event: str, *args, **kwargs) -> None:
        listeners = list(self._listeners.get(event, []))
        if not listeners:
            return
        for callback in listeners:
            try:
                result = callback(*args, **kwargs)
                if inspect.isawaitable(result):
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(result)  # type: ignore[arg-type]
                    except RuntimeError:
                        try:
                            asyncio.run(result)
                        except RuntimeError:
                            logger.exception("Failed to run async DOM listener callback")
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.error(f"DOM listener error: {exc}")

    # --------------------------------------------------------------
    def _perform_cleanup(self) -> None:
        cutoff = time.time() - self._retention_sec
        self._alerts = [a for a in self._alerts if a["timestamp"] >= cutoff]
        if len(self._alerts) > self._max_alerts:
            self._alerts = self._alerts[-self._max_alerts :]

    # --------------------------------------------------------------
    def add_listener(self, callback: Callable, event: str = "metrics") -> None:
        if not callable(callback):
            raise TypeError("callback must be callable")
        if callback in self._listeners[event]:
            return
        self._listeners[event].append(callback)

    def _load_persisted_config(self) -> Optional[Dict[str, Any]]:
        if not getattr(self, "db_manager", None):
            return None
        try:
            config = self.db_manager.load_dom_config()
        except Exception as exc:
            logger.warning(f"Failed to load DOM config from database: {exc}")
            return None
        if not isinstance(config, dict) or not config:
            return None
        return config

    def _apply_config(
        self,
        window: Any,
        max_levels: Any,
        trend_thresholds: Optional[List[float]] = None,
        *,
        persist: bool,
    ) -> None:
        def _coerce_positive_int(value: Any, fallback: int) -> int:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                return fallback
            return parsed if parsed > 0 else fallback

        target_window = _coerce_positive_int(window, int(getattr(dom_config, "window", 10000)))
        target_levels = _coerce_positive_int(max_levels, int(getattr(dom_config, "max_levels", 30)))

        if trend_thresholds is not None:
            sanitized = self._sanitize_thresholds(trend_thresholds)
        else:
            sanitized = list(getattr(dom_config, "trend_thresholds", self.trend_thresholds))
            if not sanitized:
                sanitized = list(DEFAULT_TREND_THRESHOLDS)

        dom_config.window = target_window
        dom_config.max_levels = target_levels
        dom_config.trend_thresholds = sanitized
        self.trend_thresholds = list(sanitized)

        self.processor = DOMProcessor(window=target_window, max_levels=target_levels)
        set_dom_processor(self.processor)

        try:
            import core as core_module

            core_module.dom_processor = self.processor
            core_module.dom_config = dom_config
        except Exception:
            pass

        if persist and getattr(self, "db_manager", None):
            payload = {
                "window": dom_config.window,
                "max_levels": dom_config.max_levels,
                "trend_thresholds": list(self.trend_thresholds),
            }
            try:
                self.db_manager.save_dom_config(payload)
            except Exception as exc:
                logger.warning(f"Failed to persist DOM config: {exc}")

    # --------------------------------------------------------------
    def remove_listener(self, callback: Callable, event: str = "metrics") -> None:
        listeners = self._listeners.get(event)
        if not listeners:
            return
        try:
            listeners.remove(callback)
        except ValueError:
            return
        if not listeners:
            self._listeners.pop(event, None)

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            self._perform_cleanup()

    # --------------------------------------------------------------
    def start(self, symbol: str, num_rows: int = 5) -> bool:
        if not connection_manager.is_connected():
            return False
        self._active_symbol = (symbol or "").upper()
        if self._cleanup_task is None or self._cleanup_task.done():
            try:
                self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            except RuntimeError:
                self._cleanup_task = None
        return depth_subscriber.start_depth_stream(
            symbol, lambda data, sym=symbol: self._on_depth_update(sym, data), num_rows=num_rows
        )

    # --------------------------------------------------------------
    def stop(self, symbol: str) -> bool:
        if self._active_symbol and symbol and symbol.upper() == self._active_symbol:
            self._active_symbol = None
        return depth_subscriber.stop_depth_stream(symbol)

    # --------------------------------------------------------------
    def get_metrics(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        target = (symbol or self._active_symbol or "").upper()
        active = dict(self.processor.last_metrics)
        if active:
            active_symbol = active.get("symbol")
            if not target or not active_symbol or active_symbol == target:
                return active
        if target and target in self._last_metrics_by_symbol:
            return dict(self._last_metrics_by_symbol[target])
        if target:
            cached = self._cache.get_latest_metrics(target)
            if cached:
                self._last_metrics_by_symbol[target] = dict(cached)
                return cached
        return {}

    # --------------------------------------------------------------
    def get_last_depth(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        """Return last raw depth data received from IB."""
        target = (symbol or self._active_symbol or "").upper()
        if target and target in self._last_depth_by_symbol:
            return dict(self._last_depth_by_symbol[target])
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
    def update_config(
        self,
        window: int,
        max_levels: int,
        trend_thresholds: Optional[List[float]] = None,
    ) -> None:
        self._apply_config(
            window=window,
            max_levels=max_levels,
            trend_thresholds=trend_thresholds,
            persist=True,
        )

    def _sanitize_thresholds(self, values: List[float]) -> List[float]:
        if not isinstance(values, (list, tuple)):
            values = [values]
        filtered = [
            float(v)
            for v in values
            if isinstance(v, (int, float)) and float(v) > 0
        ]
        if len(filtered) < len(values):
            # attempt to coerce non numeric entries
            for v in values:
                if isinstance(v, str):
                    try:
                        f = float(v)
                    except (TypeError, ValueError):
                        continue
                    if f > 0:
                        filtered.append(f)
        if not filtered:
            return list(DEFAULT_TREND_THRESHOLDS)
        filtered = sorted(set(filtered))
        # Ensure we always have 4 thresholds for the 4 segments
        while len(filtered) < 4:
            filtered.append(filtered[-1])
        if len(filtered) > 4:
            filtered = filtered[-4:]
        return filtered

    # --------------------------------------------------------------
    def get_events(self) -> List[str]:
        return list(self.processor.last_events)

    # --------------------------------------------------------------
    def get_history(self, symbol: str) -> Dict[str, List[Dict[str, Any]]]:
        return self._cache.get_history(symbol)

    # --------------------------------------------------------------
    def get_config(self) -> Dict[str, Any]:
        return {
            "window": int(getattr(dom_config, "window", 10000)),
            "max_levels": int(getattr(dom_config, "max_levels", 30)),
            "trend_thresholds": list(getattr(dom_config, "trend_thresholds", self.trend_thresholds))
            or list(self.trend_thresholds)
            or list(DEFAULT_TREND_THRESHOLDS),
        }


# Global service instance
_dom_service = DOMService()

def get_dom_service() -> DOMService:
    return _dom_service
