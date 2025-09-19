import time
import asyncio
import inspect
import copy
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
from src.dom_parameter_tuner import DOMParameterTuner, DEFAULT_BASE_THRESHOLDS
from src.dom_signal_tracker import SignalOutcomeTracker, resolve_outcome_config

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
        delay, threshold = resolve_outcome_config()
        self._outcome_tracker = SignalOutcomeTracker(
            self._cache,
            evaluation_delay_seconds=delay,
            price_move_threshold=threshold,
        )
        self._previous_signal_flags: DefaultDict[str, Dict[str, bool]] = defaultdict(
            lambda: {"long": False, "short": False}
        )

        persisted_config = self._load_persisted_config()
        if persisted_config:
            self._apply_config(
                window=persisted_config.get("window", dom_config.window),
                max_levels=persisted_config.get("max_levels", dom_config.max_levels),
                trend_thresholds=persisted_config.get("trend_thresholds"),
                tuner_base_thresholds=persisted_config.get("tuner_base_thresholds"),
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
        metrics["signal_long"] = bool(self.processor.last_signals.get("long"))
        metrics["signal_short"] = bool(self.processor.last_signals.get("short"))
        if symbol_key:
            state = self._previous_signal_flags[symbol_key]
            for direction in ("long", "short"):
                current_flag = metrics["signal_long"] if direction == "long" else metrics["signal_short"]
                previous_flag = state.get(direction, False)
                if current_flag and not previous_flag:
                    registration = self._outcome_tracker.register_signal(
                        symbol_key,
                        direction,
                        metrics,
                    )
                    if registration:
                        metrics.update(registration)
                    break
            state["long"] = metrics["signal_long"]
            state["short"] = metrics["signal_short"]
        timestamp = metrics.get("timestamp") or (ts_ns / 1_000_000.0)
        snapshot.setdefault("timestamp", timestamp)
        self._cache.append(symbol_key, snapshot, metrics)

        if symbol_key:
            self._outcome_tracker.evaluate_on_metrics(symbol_key, metrics)

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
        tuner_base_thresholds: Optional[Dict[str, Any]] = None,
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

        base_thresholds = self._sanitize_base_thresholds(
            tuner_base_thresholds if tuner_base_thresholds is not None else self._get_current_base_thresholds()
        )

        parameter_tuner = DOMParameterTuner(base_thresholds=base_thresholds)

        self.processor = DOMProcessor(
            window=target_window,
            max_levels=target_levels,
            parameter_tuner=parameter_tuner,
        )
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
                "tuner_base_thresholds": copy.deepcopy(base_thresholds),
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
        base_thresholds = self._get_current_base_thresholds()

        self._apply_config(
            window=window,
            max_levels=max_levels,
            trend_thresholds=trend_thresholds,
            tuner_base_thresholds=base_thresholds,
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
    def get_history(
        self,
        symbol: str,
        *,
        limit: Optional[int] = 500,
        start_time: Optional[Any] = None,
        end_time: Optional[Any] = None,
        include_snapshots: bool = True,
    ) -> Dict[str, Any]:
        return self._cache.get_history(
            symbol,
            limit=limit,
            start_time=start_time,
            end_time=end_time,
            include_snapshots=include_snapshots,
        )

    # --------------------------------------------------------------
    @staticmethod
    def _coerce_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
        if isinstance(value, str):
            try:
                return float(value.strip())
            except (TypeError, ValueError):
                return None
        return None

    # --------------------------------------------------------------
    def _normalise_thresholds(self, thresholds: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
        result: Dict[str, Dict[str, float]] = {
            "long": {},
            "short": {},
            "global": {},
        }
        if not isinstance(thresholds, dict):
            return result

        for direction in ("long", "short"):
            config = thresholds.get(direction)
            if not isinstance(config, dict):
                continue
            normalised: Dict[str, float] = {}
            for key in ("obi_min", "obi_max", "ofi_min", "ofi_max", "trend_min", "trend_max"):
                value = self._coerce_float(config.get(key))
                if value is not None:
                    normalised[key] = value
            if normalised:
                result[direction] = normalised

        global_keys = {"min_mid_price": "min_mid_price", "max_mid_price": "max_mid_price"}
        for source_key, target_key in global_keys.items():
            value = self._coerce_float(thresholds.get(source_key))
            if value is not None:
                result["global"][target_key] = value

        outcome = thresholds.get("outcome")
        if isinstance(outcome, str):
            lowered = outcome.strip().lower()
            if lowered in {"success", "hit", "win"}:
                result["global"]["outcome"] = 1.0
            elif lowered in {"failure", "lose", "loss"}:
                result["global"]["outcome"] = 0.0
            elif lowered in {"pending", "open"}:
                result["global"]["outcome"] = -1.0
        else:
            coerced = self._coerce_float(outcome)
            if coerced is not None:
                result["global"]["outcome"] = coerced

        return result

    # --------------------------------------------------------------
    def _matches_direction_thresholds(
        self,
        metric: Dict[str, Any],
        direction: str,
        thresholds: Dict[str, Dict[str, float]],
    ) -> bool:
        config = thresholds.get(direction) or {}
        if not config:
            return True

        def value_for(*keys: str) -> Optional[float]:
            for key in keys:
                value = self._coerce_float(metric.get(key))
                if value is not None:
                    return value
            return None

        obi_value = value_for("obi5", "obi")
        if "obi_min" in config:
            if obi_value is None or obi_value < config["obi_min"]:
                return False
        if "obi_max" in config:
            if obi_value is None or obi_value > config["obi_max"]:
                return False

        ofi_value = value_for("ofi")
        if "ofi_min" in config:
            if ofi_value is None or ofi_value < config["ofi_min"]:
                return False
        if "ofi_max" in config:
            if ofi_value is None or ofi_value > config["ofi_max"]:
                return False

        trend_value = value_for("trend_score")
        if "trend_min" in config:
            if trend_value is None or trend_value < config["trend_min"]:
                return False
        if "trend_max" in config:
            if trend_value is None or trend_value > config["trend_max"]:
                return False

        return True

    # --------------------------------------------------------------
    def _filter_signal_metrics(
        self,
        metrics: List[Dict[str, Any]],
        *,
        direction: Optional[str] = None,
        thresholds: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        normalised = self._normalise_thresholds(thresholds)
        min_price = normalised["global"].get("min_mid_price")
        max_price = normalised["global"].get("max_mid_price")
        outcome_filter = normalised["global"].get("outcome")

        filtered: List[Dict[str, Any]] = []
        for item in metrics:
            symbol_signal = item.get("signal_type") or item.get("signal")
            if symbol_signal not in {"long", "short"}:
                continue
            if direction and symbol_signal != direction:
                continue

            if min_price is not None or max_price is not None:
                mid_price = self._coerce_float(item.get("mid_price"))
                if min_price is not None and (mid_price is None or mid_price < min_price):
                    continue
                if max_price is not None and (mid_price is None or mid_price > max_price):
                    continue

            if outcome_filter is not None:
                stored_outcome = item.get("outcome")
                if stored_outcome is None and outcome_filter >= 0:
                    continue
                if outcome_filter == -1.0:
                    if stored_outcome is not None:
                        continue
                else:
                    try:
                        outcome_value = int(stored_outcome)
                    except (TypeError, ValueError):
                        continue
                    if outcome_value != int(outcome_filter):
                        continue

            if not self._matches_direction_thresholds(item, symbol_signal, normalised):
                continue

            filtered.append(dict(item))

        filtered.sort(key=lambda payload: self._cache._timestamp_ms(payload))
        return filtered

    # --------------------------------------------------------------
    def summarize_signals(
        self,
        symbol: str,
        *,
        start_time: Optional[Any] = None,
        end_time: Optional[Any] = None,
        limit: Optional[int] = None,
        direction: Optional[str] = None,
        thresholds: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        history = self._cache.get_history(
            symbol,
            limit=limit,
            start_time=start_time,
            end_time=end_time,
            include_snapshots=False,
        )
        metrics = history.get("metrics", []) if isinstance(history, dict) else []
        filtered = self._filter_signal_metrics(metrics, direction=direction, thresholds=thresholds)

        total = len(filtered)
        longs = sum(1 for item in filtered if item.get("signal_type") == "long")
        shorts = sum(1 for item in filtered if item.get("signal_type") == "short")
        hits = sum(1 for item in filtered if item.get("outcome") == 1)
        failures = sum(1 for item in filtered if item.get("outcome") == 0)
        pending = sum(1 for item in filtered if item.get("outcome") is None)
        evaluated = hits + failures
        success_rate = hits / evaluated if evaluated else None

        window_start = filtered[0].get("timestamp") if filtered else None
        window_end = filtered[-1].get("timestamp") if filtered else None

        return {
            "symbol": symbol.upper(),
            "total_signals": total,
            "long_signals": longs,
            "short_signals": shorts,
            "hits": hits,
            "failures": failures,
            "pending": pending,
            "success_rate": success_rate,
            "history_window": {"start": window_start, "end": window_end},
        }

    # --------------------------------------------------------------
    def query_signals(
        self,
        symbol: str,
        *,
        start_time: Optional[Any] = None,
        end_time: Optional[Any] = None,
        limit: Optional[int] = None,
        page: int = 1,
        page_size: int = 50,
        direction: Optional[str] = None,
        thresholds: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        page = max(int(page or 1), 1)
        page_size = max(min(int(page_size or 50), 500), 1)

        history = self._cache.get_history(
            symbol,
            limit=limit,
            start_time=start_time,
            end_time=end_time,
            include_snapshots=False,
        )
        metrics = history.get("metrics", []) if isinstance(history, dict) else []
        filtered = self._filter_signal_metrics(metrics, direction=direction, thresholds=thresholds)

        total = len(filtered)
        total_pages = (total + page_size - 1) // page_size if total else 0
        start_index = (page - 1) * page_size
        end_index = start_index + page_size
        items = filtered[start_index:end_index] if start_index < total else []

        return {
            "symbol": symbol.upper(),
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "items": items,
        }

    # --------------------------------------------------------------
    def get_config(self) -> Dict[str, Any]:
        return {
            "window": int(getattr(dom_config, "window", 10000)),
            "max_levels": int(getattr(dom_config, "max_levels", 30)),
            "trend_thresholds": list(getattr(dom_config, "trend_thresholds", self.trend_thresholds))
            or list(self.trend_thresholds)
            or list(DEFAULT_TREND_THRESHOLDS),
            "tuner_base_thresholds": self._get_current_base_thresholds(),
        }

    # --------------------------------------------------------------
    def apply_tuner_base_thresholds(
        self,
        base_thresholds: Dict[str, Any],
        *,
        persist: bool = True,
    ) -> Dict[str, Dict[str, float]]:
        sanitized = self._sanitize_base_thresholds(base_thresholds)
        current_config = self.get_config()
        self._apply_config(
            window=current_config["window"],
            max_levels=current_config["max_levels"],
            trend_thresholds=current_config.get("trend_thresholds"),
            tuner_base_thresholds=sanitized,
            persist=persist,
        )
        return sanitized

    # --------------------------------------------------------------
    def get_tuner_base_thresholds(self) -> Dict[str, Dict[str, float]]:
        return self._get_current_base_thresholds()

    # --------------------------------------------------------------
    def _get_current_base_thresholds(self) -> Dict[str, Dict[str, float]]:
        tuner = getattr(self.processor, "parameter_tuner", None)
        if tuner and getattr(tuner, "base_thresholds", None):
            return copy.deepcopy(tuner.base_thresholds)
        return copy.deepcopy(DEFAULT_BASE_THRESHOLDS)

    def _sanitize_base_thresholds(
        self, values: Optional[Dict[str, Any]]
    ) -> Dict[str, Dict[str, float]]:
        sanitized = copy.deepcopy(DEFAULT_BASE_THRESHOLDS)
        if not isinstance(values, dict):
            return sanitized

        def _coerce(value: Any, fallback: float) -> float:
            try:
                return float(value)
            except (TypeError, ValueError):
                return float(fallback)

        for section, defaults in DEFAULT_BASE_THRESHOLDS.items():
            target = sanitized.setdefault(section, {})
            incoming = values.get(section) if isinstance(values.get(section), dict) else {}
            if not isinstance(target, dict):
                continue
            for key, default_value in defaults.items():
                source_value = None
                if isinstance(incoming, dict):
                    source_value = incoming.get(key)
                target[key] = _coerce(source_value, float(default_value))
        return sanitized


# Global service instance
_dom_service = DOMService()

def get_dom_service() -> DOMService:
    return _dom_service
