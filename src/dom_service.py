import os
import time
import asyncio
import inspect
import copy
import threading
import atexit
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Lock
from typing import Any, Awaitable, Callable, DefaultDict, Dict, List, Mapping, Optional
from zoneinfo import ZoneInfo

from loguru import logger

from src.data_layer import (
    DataSubscriptionRequest,
    DataSubscriptionToken,
    DataSourceError,
    EventEnvelope,
)
from src.data_layer.event_bus import BusSubscriptionToken
from src.data_layer.bootstrap import get_data_source_manager
from src.connection_manager import connection_manager as _GLOBAL_CONNECTION_MANAGER
from src.dom_cache import DomCacheManager
from src.dom_dependencies import (
    dom_config,
    get_dom_processor,
    set_dom_processor,
)
from src.dom_processor import DOMProcessor
from src.dom_parameter_tuner import (
    DOMParameterTuner,
    DEFAULT_BASE_THRESHOLDS,
    DEFAULT_FEATURE_THRESHOLDS,
)
from src.dom_signal_tracker import SignalOutcomeTracker, resolve_outcome_config
from src.dom_stats import DomStatsAggregator
from src.dom_persistence import load_dom_config, save_dom_config

DEFAULT_TREND_THRESHOLDS = [0.5, 0.7, 0.85, 0.95]


TUNER_STATS_WINDOW_ENV = "DOM_TUNER_STATS_WINDOW"
TUNER_PENALTY_DECAY_ENV = "DOM_TUNER_PENALTY_DECAY"
TUNER_PENALTY_STEP_ENV = "DOM_TUNER_PENALTY_STEP"
TUNER_MAX_PENALTY_ENV = "DOM_TUNER_MAX_PENALTY"
TUNER_REVERSAL_WINDOW_ENV = "DOM_TUNER_REVERSAL_WINDOW_SEC"
TUNER_SESSION_TZ_ENV = "DOM_TUNER_SESSION_TZ"

DEFAULT_TUNER_PARAMETERS = {
    "stats_window": 900,
    "penalty_decay": 0.98,
    "penalty_step": 0.02,
    "max_penalty": 0.15,
    "reversal_window_sec": 60,
    "session_timezone": "America/New_York",
}

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


class DOMService:
    """Service layer wrapping DOM processing and alerts."""

    def __init__(self) -> None:
        self.processor: DOMProcessor = get_dom_processor()
        self._alerts: List[Dict[str, Any]] = []
        self._next_alert_id: int = 1
        self._max_alerts = 100
        self._retention_sec = 300
        self._cleanup_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Store last raw depth update for monitoring
        self._last_depth: Dict[str, Any] = {}
        self._last_depth_by_symbol: Dict[str, Dict[str, Any]] = {}
        self._last_metrics_by_symbol: Dict[str, Dict[str, Any]] = {}
        self._active_symbols: Dict[str, str] = {}
        self._active_symbol: Optional[str] = None
        self._active_symbol_original: Optional[str] = None
        self._processors: Dict[str, DOMProcessor] = {}
        self._cache = DomCacheManager()
        self._stats = DomStatsAggregator(self._cache)
        self.trend_thresholds: List[float] = list(
            getattr(dom_config, "trend_thresholds", DEFAULT_TREND_THRESHOLDS)
        ) or list(DEFAULT_TREND_THRESHOLDS)
        self._listeners: DefaultDict[str, List[Callable]] = defaultdict(list)
        self._tuner_parameters: Dict[str, Any] = copy.deepcopy(DEFAULT_TUNER_PARAMETERS)
        delay, threshold = resolve_outcome_config()
        self._outcome_tracker = SignalOutcomeTracker(
            self._cache,
            evaluation_delay_seconds=delay,
            price_move_threshold=threshold,
        )
        self._previous_signal_flags: DefaultDict[str, Dict[str, bool]] = defaultdict(
            lambda: {"long": False, "short": False}
        )
        self._data_layer_manager = None
        self._data_layer_tokens: Dict[str, DataSubscriptionToken] = {}
        self._data_layer_subscription: Optional[BusSubscriptionToken] = None
        self._last_start_error: Optional[str] = None
        self._last_start_error_status: int = 200
        self._start_tasks: Dict[str, asyncio.Task[bool]] = {}
        self._start_tasks_lock = Lock()
        self._shutdown_event = threading.Event()
        self._auto_start_symbol: Optional[str] = None
        self._auto_start_num_rows: int = max(
            5, min(int(getattr(dom_config, "max_levels", 10) or 10), 30)
        )
        self._auto_start_worker_started = False
        self._processor_window: int = int(getattr(dom_config, "window", 10000))
        self._processor_max_levels: int = int(getattr(dom_config, "max_levels", 30))
        self._current_base_thresholds: Dict[str, Dict[str, float]] = copy.deepcopy(
            DEFAULT_BASE_THRESHOLDS
        )
        self._current_feature_thresholds: Dict[str, Dict[str, float]] = copy.deepcopy(
            DEFAULT_FEATURE_THRESHOLDS
        )

        persisted_config = load_dom_config()
        if persisted_config:
            self._apply_config(
                window=persisted_config.get("window", dom_config.window),
                max_levels=persisted_config.get("max_levels", dom_config.max_levels),
                trend_thresholds=persisted_config.get("trend_thresholds"),
                tuner_base_thresholds=persisted_config.get("tuner_base_thresholds"),
                tuner_feature_thresholds=persisted_config.get("tuner_feature_thresholds"),
                tuner_parameters=persisted_config.get("tuner_parameters"),
                persist=False,
            )
        else:
            self._apply_config(
                window=getattr(dom_config, "window", 10000),
                max_levels=getattr(dom_config, "max_levels", 30),
                trend_thresholds=list(self.trend_thresholds),
                tuner_base_thresholds=None,
                tuner_feature_thresholds=None,
                tuner_parameters=None,
                persist=False,
            )

        atexit.register(self._handle_process_shutdown)

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

        processor = self._ensure_processor_for_symbol(symbol_key)
        if processor is not self.processor:
            self.processor = processor
            set_dom_processor(processor)

        processor.on_depth_update(snapshot)
        metrics = dict(processor.last_metrics)
        if symbol_key:
            metrics["symbol"] = symbol_key
            self._last_metrics_by_symbol[symbol_key] = metrics
        metrics["signal_long"] = bool(processor.last_signals.get("long"))
        metrics["signal_short"] = bool(processor.last_signals.get("short"))
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
            self._stats.mark_dirty(symbol_key)

        if symbol_key:
            updates = self._outcome_tracker.evaluate_on_metrics(symbol_key, metrics)
            if updates:
                self._stats.mark_dirty(symbol_key)

        ts = time.time()
        for evt in processor.last_events:
            self._alerts.append({"id": self._next_alert_id, "event": evt, "timestamp": ts})
            self._next_alert_id += 1
        self._perform_cleanup()
        self._dispatch_event("metrics", symbol_key, metrics, snapshot)

    # --------------------------------------------------------------
    def get_stats(
        self,
        symbol: str,
        *,
        lookback_seconds: Optional[int] = None,
        start_time: Optional[Any] = None,
        end_time: Optional[Any] = None,
        max_points: Optional[int] = None,
    ) -> Dict[str, Any]:
        if not symbol:
            return {
                "success": True,
                "symbol": symbol,
                "stats": {
                    "total": 0,
                    "long": 0,
                    "short": 0,
                    "evaluated": 0,
                    "success": 0,
                    "failure": 0,
                    "pending": 0,
                    "precision": None,
                    "avg_delay_seconds": None,
                },
                "latest_signal": None,
                "chart_metrics": [],
                "window": {
                    "start": None,
                    "end": None,
                    "lookback_seconds": lookback_seconds,
                },
                "metadata": {
                    "version": DomStatsAggregator.VERSION,
                    "rebuilt_at": None,
                    "source_records": 0,
                    "cached_metrics": 0,
                    "metrics_offset": 0,
                    "max_timestamp": None,
                },
            }

        return self._stats.get_stats(
            symbol,
            lookback_seconds=lookback_seconds,
            start=start_time,
            end=end_time,
            max_points=max_points,
        )

    async def get_stats_async(
        self,
        symbol: str,
        *,
        lookback_seconds: Optional[int] = None,
        start_time: Optional[Any] = None,
        end_time: Optional[Any] = None,
        max_points: Optional[int] = None,
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(
            self.get_stats,
            symbol,
            lookback_seconds=lookback_seconds,
            start_time=start_time,
            end_time=end_time,
            max_points=max_points,
        )

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

    def _resolve_tuner_parameters(
        self, overrides: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        params = copy.deepcopy(DEFAULT_TUNER_PARAMETERS)

        def _coerce_int(value: Any, minimum: int, fallback: int) -> int:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                return fallback
            return parsed if parsed >= minimum else fallback

        def _coerce_float(value: Any, minimum: float, fallback: float, maximum: Optional[float] = None) -> float:
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                return fallback
            if maximum is not None:
                parsed = min(parsed, maximum)
            return parsed if parsed >= minimum else fallback

        env_values = {
            "stats_window": os.getenv(TUNER_STATS_WINDOW_ENV),
            "penalty_decay": os.getenv(TUNER_PENALTY_DECAY_ENV),
            "penalty_step": os.getenv(TUNER_PENALTY_STEP_ENV),
            "max_penalty": os.getenv(TUNER_MAX_PENALTY_ENV),
            "reversal_window_sec": os.getenv(TUNER_REVERSAL_WINDOW_ENV),
            "session_timezone": os.getenv(TUNER_SESSION_TZ_ENV),
        }

        if env_values["stats_window"] is not None:
            params["stats_window"] = _coerce_int(env_values["stats_window"], 1, params["stats_window"])
        if env_values["penalty_decay"] is not None:
            params["penalty_decay"] = _coerce_float(
                env_values["penalty_decay"], 0.0, params["penalty_decay"], maximum=1.0
            )
        if env_values["penalty_step"] is not None:
            params["penalty_step"] = _coerce_float(env_values["penalty_step"], 0.0, params["penalty_step"])
        if env_values["max_penalty"] is not None:
            params["max_penalty"] = _coerce_float(env_values["max_penalty"], 0.0, params["max_penalty"])
        if env_values["reversal_window_sec"] is not None:
            params["reversal_window_sec"] = _coerce_int(
                env_values["reversal_window_sec"], 1, params["reversal_window_sec"]
            )
        if env_values["session_timezone"]:
            params["session_timezone"] = str(env_values["session_timezone"])

        source = overrides
        if isinstance(source, dict):
            if "stats_window" in source:
                params["stats_window"] = _coerce_int(source.get("stats_window"), 1, params["stats_window"])
            if "penalty_decay" in source:
                params["penalty_decay"] = _coerce_float(
                    source.get("penalty_decay"), 0.0, params["penalty_decay"], maximum=1.0
                )
            if "penalty_step" in source:
                params["penalty_step"] = _coerce_float(
                    source.get("penalty_step"), 0.0, params["penalty_step"]
                )
            if "max_penalty" in source:
                params["max_penalty"] = _coerce_float(source.get("max_penalty"), 0.0, params["max_penalty"])
            if "reversal_window_sec" in source:
                params["reversal_window_sec"] = _coerce_int(
                    source.get("reversal_window_sec"), 1, params["reversal_window_sec"]
                )
            if "session_timezone" in source and source.get("session_timezone"):
                params["session_timezone"] = str(source.get("session_timezone"))

        self._tuner_parameters = copy.deepcopy(params)
        return copy.deepcopy(params)

    def _apply_config(
        self,
        window: Any,
        max_levels: Any,
        trend_thresholds: Optional[List[float]] = None,
        tuner_base_thresholds: Optional[Dict[str, Any]] = None,
        tuner_feature_thresholds: Optional[Dict[str, Any]] = None,
        tuner_parameters: Optional[Dict[str, Any]] = None,
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

        feature_thresholds = self._sanitize_feature_thresholds(
            tuner_feature_thresholds
            if tuner_feature_thresholds is not None
            else self._get_current_feature_thresholds()
        )

        tuner_params = self._resolve_tuner_parameters(overrides=tuner_parameters)

        self._processor_window = int(target_window)
        self._processor_max_levels = int(target_levels)
        self._current_base_thresholds = copy.deepcopy(base_thresholds)
        self._current_feature_thresholds = copy.deepcopy(feature_thresholds)

        parameter_tuner = DOMParameterTuner(
            base_thresholds=self._current_base_thresholds,
            feature_thresholds=self._current_feature_thresholds,
            stats_window=int(tuner_params["stats_window"]),
            penalty_decay=float(tuner_params["penalty_decay"]),
            penalty_step=float(tuner_params["penalty_step"]),
            max_penalty=float(tuner_params["max_penalty"]),
            reversal_window_sec=int(tuner_params["reversal_window_sec"]),
            session_timezone=str(tuner_params["session_timezone"]),
        )

        self.processor = DOMProcessor(
            window=self._processor_window,
            max_levels=self._processor_max_levels,
            parameter_tuner=parameter_tuner,
        )
        self._processors = {}
        if self._active_symbol:
            self._processors[self._active_symbol] = self.processor
        set_dom_processor(self.processor)

        logger.info(
            "DOM configuration applied | window={} max_levels={} tuner_params={}",
            target_window,
            target_levels,
            tuner_params,
        )

        try:
            import core as core_module

            core_module.dom_processor = self.processor
            core_module.dom_config = dom_config
        except Exception:
            pass

        if persist:
            payload = {
                "window": dom_config.window,
                "max_levels": dom_config.max_levels,
                "trend_thresholds": list(self.trend_thresholds),
                "tuner_base_thresholds": copy.deepcopy(base_thresholds),
                "tuner_feature_thresholds": copy.deepcopy(feature_thresholds),
                "tuner_parameters": copy.deepcopy(self._tuner_parameters),
            }
            save_dom_config(payload)

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
    def _ensure_cleanup_task(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        if self._cleanup_task is not None and not self._cleanup_task.done():
            return

        loop = loop or self._loop
        if loop is None or loop.is_closed():
            return

        def _schedule() -> None:
            if self._cleanup_task is None or self._cleanup_task.done():
                self._cleanup_task = loop.create_task(self._cleanup_loop())

        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if running_loop is loop:
            _schedule()
        else:
            loop.call_soon_threadsafe(_schedule)

    def _set_primary_active_symbol(self, symbol_key: Optional[str]) -> None:
        if symbol_key and symbol_key in self._active_symbols:
            self._active_symbol = symbol_key
            self._active_symbol_original = self._active_symbols[symbol_key]
            processor = self._get_processor_for_symbol(symbol_key)
            if processor is None:
                processor = self._ensure_processor_for_symbol(symbol_key)
            if processor is not None and processor is not self.processor:
                self.processor = processor
                set_dom_processor(processor)
            return

        if self._active_symbols:
            first_key = next(iter(self._active_symbols))
            self._active_symbol = first_key
            self._active_symbol_original = self._active_symbols[first_key]
            processor = self._get_processor_for_symbol(first_key)
            if processor is None:
                processor = self._ensure_processor_for_symbol(first_key)
            if processor is not None and processor is not self.processor:
                self.processor = processor
                set_dom_processor(processor)
            return

        self._active_symbol = None
        self._active_symbol_original = None
        if not self._processors:
            self.processor = self._create_processor_instance()
        else:
            self.processor = next(iter(self._processors.values()))
        set_dom_processor(self.processor)

    def _register_active_symbol(self, symbol_key: str, original: str) -> None:
        if not symbol_key:
            return

        self._active_symbols[symbol_key] = original or symbol_key
        processor = self._ensure_processor_for_symbol(symbol_key)
        if processor is not None and processor is not self.processor:
            self.processor = processor
            set_dom_processor(processor)
        self._set_primary_active_symbol(symbol_key)

    def _unregister_active_symbol(self, symbol_key: str) -> Optional[str]:
        if not symbol_key:
            return None

        removed = self._active_symbols.pop(symbol_key, None)
        removed_processor = self._processors.pop(symbol_key, None)
        if removed is not None or self._active_symbol == symbol_key:
            self._set_primary_active_symbol(None)
        if removed_processor is not None and removed_processor is self.processor:
            replacement: Optional[DOMProcessor] = None
            if self._active_symbol and self._active_symbol in self._processors:
                replacement = self._processors[self._active_symbol]
            elif self._processors:
                replacement = next(iter(self._processors.values()))
            if replacement is None:
                replacement = self._create_processor_instance()
            self.processor = replacement
            set_dom_processor(self.processor)
        self._previous_signal_flags.pop(symbol_key, None)
        return removed

    def _create_processor_instance(self) -> DOMProcessor:
        tuner_params = self._tuner_parameters or DEFAULT_TUNER_PARAMETERS
        parameter_tuner = DOMParameterTuner(
            base_thresholds=copy.deepcopy(self._current_base_thresholds),
            feature_thresholds=copy.deepcopy(self._current_feature_thresholds),
            stats_window=int(tuner_params["stats_window"]),
            penalty_decay=float(tuner_params["penalty_decay"]),
            penalty_step=float(tuner_params["penalty_step"]),
            max_penalty=float(tuner_params["max_penalty"]),
            reversal_window_sec=int(tuner_params["reversal_window_sec"]),
            session_timezone=str(tuner_params["session_timezone"]),
        )
        return DOMProcessor(
            window=int(self._processor_window),
            max_levels=int(self._processor_max_levels),
            parameter_tuner=parameter_tuner,
        )

    def _ensure_processor_for_symbol(self, symbol_key: str) -> DOMProcessor:
        if not symbol_key:
            return self.processor
        existing = self._processors.get(symbol_key)
        if existing is not None:
            return existing
        if not self._processors:
            processor = self.processor
        else:
            processor = self._create_processor_instance()
        self._processors[symbol_key] = processor
        return processor

    def _get_processor_for_symbol(
        self, symbol_key: Optional[str]
    ) -> Optional[DOMProcessor]:
        if symbol_key:
            processor = self._processors.get(symbol_key)
            if processor is not None:
                return processor
        if self._active_symbol and self._active_symbol in self._processors:
            return self._processors[self._active_symbol]
        if self._processors:
            return next(iter(self._processors.values()))
        return self.processor

    def _ensure_data_layer_manager(self):
        if self._data_layer_manager is None:
            try:
                self._data_layer_manager = get_data_source_manager()
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.error("Failed to initialise data layer manager: {}", exc)
                self._last_start_error = str(exc) or "Failed to initialise data layer"
                self._last_start_error_status = 500
                return None
        return self._data_layer_manager

    def _ensure_data_layer_subscription(self) -> None:
        manager = self._ensure_data_layer_manager()
        if manager is None:
            return
        if self._data_layer_subscription is not None:
            return

        self._data_layer_subscription = manager.event_bus.subscribe(
            "dom.depth", self._handle_dom_depth_event
        )

    async def _handle_dom_depth_event(self, envelope: EventEnvelope) -> None:
        symbol = (envelope.symbol or "").upper()
        if not symbol:
            return

        payload = dict(envelope.payload or {})
        payload.setdefault("symbol", symbol)
        payload.setdefault("ts_ns", envelope.ts_ns or time.time_ns())
        payload["timestamp"] = _to_local_iso(payload.get("timestamp"))
        self._on_depth_update(symbol, payload)

    def _run_sync_coroutine(self, coro: Awaitable[bool]) -> bool:
        """Execute *coro* from synchronous contexts safely."""

        loop = self._loop
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if (
            loop
            and loop is not running_loop
            and loop.is_running()
            and not loop.is_closed()
        ):
            return bool(asyncio.run_coroutine_threadsafe(coro, loop).result())

        if running_loop and running_loop.is_running():
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(asyncio.run, coro)
                return bool(future.result())

        return bool(asyncio.run(coro))

    async def _start_stream_unified(
        self, symbol: str, num_rows: int = 5, *, force_restart: bool = False
    ) -> bool:
        self._last_start_error = None
        self._last_start_error_status = 200
        symbol_key = (symbol or "").upper()
        if not symbol_key:
            self._last_start_error = "Missing symbol"
            self._last_start_error_status = 400
            return False

        logger.debug("Preparing to start DOM depth stream", symbol=symbol_key, num_rows=num_rows)

        manager = self._ensure_data_layer_manager()
        if manager is None:
            return False

        self._ensure_data_layer_subscription()

        if force_restart:
            token = self._data_layer_tokens.pop(symbol_key, None)
            if token is not None:
                try:
                    await manager.unsubscribe(token)
                except Exception as exc:  # pragma: no cover - defensive guard
                    logger.debug(
                        "Failed to unsubscribe existing DOM stream during restart",
                        symbol=symbol_key,
                        error=str(exc),
                    )
        elif symbol_key in self._data_layer_tokens:
            return True

        request = DataSubscriptionRequest(
            channel="dom.depth",
            symbol=symbol_key,
            options={"num_rows": num_rows, "force_restart": force_restart},
        )
        try:
            token = await manager.subscribe(request)
        except DataSourceError as exc:
            message = str(exc) or "Failed to start DOM depth stream"
            self._last_start_error = message
            self._last_start_error_status = 500
            logger.warning(
                "Unified DOM depth subscription failed",
                symbol=symbol_key,
                error=message,
            )
            return False

        self._data_layer_tokens[symbol_key] = token
        self._active_symbol = symbol_key
        self._active_symbol_original = symbol
        self._register_active_symbol(symbol_key, symbol)
        logger.info("Unified DOM depth stream started", symbol=symbol_key, num_rows=num_rows)
        return True

    def ensure_stream_running(
        self,
        symbol: Optional[str],
        *,
        num_rows: Optional[int] = None,
        force_restart: bool = False,
    ) -> bool:
        """Best-effort helper to keep a DOM stream active without blocking."""

        symbol_code = (symbol or "").strip().upper()
        if not symbol_code:
            return False
        if self.is_stream_active(symbol_code) or self.is_start_in_progress(symbol_code):
            return True

        target_rows = int(num_rows or self._auto_start_num_rows or 5)
        try:
            task = self.request_stream_start(
                symbol_code, num_rows=target_rows, force_restart=force_restart
            )
        except RuntimeError:
            try:
                return self.start(
                    symbol_code, num_rows=target_rows, force_restart=force_restart
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.warning(
                    "DOM ensure_stream_running failed in blocking start",
                    symbol=symbol_code,
                    error=str(exc),
                )
                return False
        else:
            if isinstance(task, asyncio.Task):
                task.add_done_callback(lambda fut: None)
            return True

    async def start_stream(
        self, symbol: str, num_rows: int = 5, *, force_restart: bool = False
    ) -> bool:
        loop = asyncio.get_running_loop()
        self._loop = loop
        self._ensure_cleanup_task(loop)
        manager = self._ensure_data_layer_manager()
        if manager is None:
            return False
        return await self._start_stream_unified(
            symbol, num_rows, force_restart=force_restart
        )

    def request_stream_start(
        self, symbol: str, num_rows: int = 5, *, force_restart: bool = False
    ) -> asyncio.Task[bool]:
        """Schedule a DOM stream start and return the task without waiting."""

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = self._loop
            if loop is None or loop.is_closed():
                raise
        else:
            self._loop = loop

        self._ensure_cleanup_task(loop)

        symbol_key = (symbol or "").upper()

        with self._start_tasks_lock:
            existing = self._start_tasks.get(symbol_key)
            if existing and not existing.done():
                if force_restart:
                    existing.cancel()
                else:
                    return existing

            task = loop.create_task(
                self.start_stream(symbol, num_rows, force_restart=force_restart)
            )
            self._start_tasks[symbol_key] = task

        task.add_done_callback(lambda fut, sym=symbol_key: self._on_start_task_done(sym, fut))
        return task

    def start(
        self, symbol: str, num_rows: int = 5, *, force_restart: bool = False
    ) -> bool:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = self._loop
        else:
            self._loop = loop
        self._ensure_cleanup_task(loop)
        manager = self._ensure_data_layer_manager()
        if manager is None:
            return False
        return self._run_sync_coroutine(
            self._start_stream_unified(
                symbol, num_rows, force_restart=force_restart
            )
        )

    def get_last_start_error(self) -> tuple[Optional[str], int]:
        return self._last_start_error, self._last_start_error_status

    async def stop_stream(self, symbol: str) -> bool:
        symbol_key = (symbol or "").upper()
        had_pending = False
        with self._start_tasks_lock:
            task = self._start_tasks.get(symbol_key)
            if task and not task.done():
                task.cancel()
                had_pending = True
            self._start_tasks.pop(symbol_key, None)

        removed_symbol = self._unregister_active_symbol(symbol_key)
        manager = self._ensure_data_layer_manager()
        token = self._data_layer_tokens.pop(symbol_key, None)
        unsubscribed = False
        if manager and token is not None:
            try:
                await manager.unsubscribe(token)
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.debug(
                    "Unified DOM unsubscribe failed", symbol=symbol_key, error=str(exc)
                )
            else:
                unsubscribed = True
        if manager and not self._data_layer_tokens and self._data_layer_subscription:
            manager.event_bus.unsubscribe(self._data_layer_subscription)
            self._data_layer_subscription = None
        return bool(unsubscribed or had_pending or removed_symbol)

    def stop(self, symbol: str) -> bool:
        return self._run_sync_coroutine(self.stop_stream(symbol))

    @property
    def auto_start_symbol(self) -> Optional[str]:
        """Return the symbol configured for automatic start, if any."""

        return self._auto_start_symbol

    def ensure_auto_start_worker(self) -> bool:
        """Start the auto-start worker if it has not been launched yet.

        Returns ``True`` when the worker is scheduled during this call.
        """

        if not self._auto_start_symbol:
            return False
        if self._auto_start_worker_started:
            return False
        self._start_auto_start_worker()
        return True

    def _handle_process_shutdown(self) -> None:
        try:
            self._shutdown_event.set()
        except Exception:
            pass

    def _start_auto_start_worker(self) -> None:
        if not self._auto_start_symbol or self._auto_start_worker_started:
            return

        self._auto_start_worker_started = True

        def _worker() -> None:
            symbol = self._auto_start_symbol
            if not symbol:
                return
            attempts = 0
            max_attempts = 40
            while attempts < max_attempts:
                if self._shutdown_event.is_set():
                    return
                attempts += 1
                logger.debug(
                    "Auto-start worker iteration",
                    symbol=symbol,
                    attempt=attempts,
                    max_attempts=max_attempts,
                )
                if self.is_stream_active(symbol) or self.is_start_in_progress(symbol):
                    return
                try:
                    if self.start(symbol, num_rows=self._auto_start_num_rows):
                        logger.info("Auto-started DOM depth stream", symbol=symbol)
                        return
                except Exception as exc:
                    if self._shutdown_event.is_set():
                        return
                    message = str(exc)
                    if isinstance(exc, RuntimeError) and "interpreter shutdown" in message.lower():
                        return
                    logger.warning(
                        "Auto-start DOM stream attempt failed",
                        symbol=symbol,
                        error=message,
                    )
                if self._shutdown_event.wait(3.0):
                    return

        threading.Thread(
            target=_worker,
            name="DOMAutoStart",
            daemon=True,
        ).start()

    def is_start_in_progress(self, symbol: Optional[str] = None) -> bool:
        symbol_key = (symbol or "").upper()
        with self._start_tasks_lock:
            if symbol_key:
                task = self._start_tasks.get(symbol_key)
                return bool(task and not task.done())
            return any(task and not task.done() for task in self._start_tasks.values())

    def is_stream_active(self, symbol: Optional[str] = None) -> bool:
        symbol_key = (symbol or "").upper()
        lookup_key = symbol_key or self._active_symbol or ""

        candidates: List[str] = []
        if symbol:
            candidates.append(symbol)
        if lookup_key:
            stored = self._active_symbols.get(lookup_key)
            if stored:
                candidates.append(stored)
            candidates.append(lookup_key)

        seen: set[str] = set()
        for candidate in candidates:
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            if candidate in self._data_layer_tokens:
                return True
            if candidate in self._active_symbols:
                return True

        if symbol_key:
            return symbol_key in self._data_layer_tokens or symbol_key in self._active_symbols
        return bool(self._data_layer_tokens or self._active_symbols)

    def _on_start_task_done(self, symbol: str, fut: "asyncio.Future[bool]") -> None:
        with self._start_tasks_lock:
            current = self._start_tasks.get(symbol)
            if current is fut:
                self._start_tasks.pop(symbol, None)
        try:
            result = fut.result()
        except asyncio.CancelledError:
            self._unregister_active_symbol(symbol)
            return
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.error(
                "DOM depth stream start task failed", symbol=symbol, error=str(exc)
            )
            self._last_start_error = str(exc) or "Failed to start DOM depth stream"
            self._last_start_error_status = 500
            self._unregister_active_symbol(symbol)
        else:
            if not result:
                self._unregister_active_symbol(symbol)

    # --------------------------------------------------------------
    def get_metrics(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        target = (symbol or self._active_symbol or "").upper()
        processor = self._get_processor_for_symbol(target if target else None)
        if processor and processor.last_metrics:
            active = dict(processor.last_metrics)
            if target and not active.get("symbol"):
                active["symbol"] = target
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
            tuner_parameters=self._tuner_parameters,
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
    def get_events(self, symbol: Optional[str] = None) -> List[str]:
        target = (symbol or self._active_symbol or "").upper()
        processor = self._get_processor_for_symbol(target if target else None)
        if processor and processor.last_events:
            return list(processor.last_events)
        return []

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
    def ingest_backfill_snapshot(
        self,
        symbol: str,
        snapshot: Mapping[str, Any],
        *,
        source: str = "backfill",
    ) -> None:
        if not symbol or not snapshot:
            return

        symbol_key = symbol.strip().upper()
        if not symbol_key:
            return

        bids = [dict(entry) for entry in snapshot.get("bids", []) or []]
        asks = [dict(entry) for entry in snapshot.get("asks", []) or []]

        ts_ns = snapshot.get("timestamp_ns") or snapshot.get("ts_ns")
        if not isinstance(ts_ns, int):
            timestamp_value = snapshot.get("timestamp")
            parsed_ns: Optional[int] = None
            if isinstance(timestamp_value, (int, float)):
                parsed_ns = int(float(timestamp_value) * 1_000_000_000)
            elif isinstance(timestamp_value, str):
                text = timestamp_value.strip()
                if text.endswith("Z"):
                    text = text[:-1] + "+00:00"
                try:
                    dt_candidate = datetime.fromisoformat(text)
                except Exception:  # pragma: no cover - defensive guard
                    dt_candidate = None
                if dt_candidate is not None:
                    if dt_candidate.tzinfo is None:
                        dt_candidate = dt_candidate.replace(tzinfo=UTC_TZ)
                    parsed_ns = int(dt_candidate.timestamp() * 1_000_000_000)
            ts_ns = parsed_ns if parsed_ns is not None else time.time_ns()

        timestamp_iso = _to_local_iso(snapshot.get("timestamp") or (ts_ns / 1_000_000))
        normalized_snapshot = {
            "symbol": symbol_key,
            "bids": bids,
            "asks": asks,
            "timestamp": timestamp_iso,
            "timestamp_ns": ts_ns,
            "ts_ns": ts_ns,
        }

        metrics_payload = {
            "symbol": symbol_key,
            "timestamp": ts_ns / 1_000_000.0,
            "source": source,
            "snapshot_type": "dom",
            "bid_levels": len(bids),
            "ask_levels": len(asks),
        }

        self._cache.append(symbol_key, normalized_snapshot, metrics_payload)
        self._stats.mark_dirty(symbol_key)

    # --------------------------------------------------------------
    def get_config(self) -> Dict[str, Any]:
        return {
            "window": int(getattr(dom_config, "window", 10000)),
            "max_levels": int(getattr(dom_config, "max_levels", 30)),
            "trend_thresholds": list(getattr(dom_config, "trend_thresholds", self.trend_thresholds))
            or list(self.trend_thresholds)
            or list(DEFAULT_TREND_THRESHOLDS),
            "tuner_base_thresholds": self._get_current_base_thresholds(),
            "tuner_feature_thresholds": self._get_current_feature_thresholds(),
            "tuner_parameters": copy.deepcopy(self._tuner_parameters),
        }

    # --------------------------------------------------------------
    def apply_tuner_thresholds(
        self,
        base_thresholds: Dict[str, Any],
        *,
        feature_thresholds: Optional[Dict[str, Any]] = None,
        persist: bool = True,
        trend_thresholds: Optional[List[float]] = None,
    ) -> Dict[str, Dict[str, Dict[str, float]]]:
        sanitized_base = self._sanitize_base_thresholds(base_thresholds)
        sanitized_features = (
            self._sanitize_feature_thresholds(feature_thresholds)
            if feature_thresholds is not None
            else self._get_current_feature_thresholds()
        )
        current_config = self.get_config()
        logger.info(
            "Applying tuner thresholds | base={} features={}",
            sanitized_base,
            sanitized_features,
        )
        self._apply_config(
            window=current_config["window"],
            max_levels=current_config["max_levels"],
            trend_thresholds=trend_thresholds or current_config.get("trend_thresholds"),
            tuner_base_thresholds=sanitized_base,
            tuner_feature_thresholds=sanitized_features,
            tuner_parameters=self._tuner_parameters,
            persist=persist,
        )
        return {
            "base": sanitized_base,
            "features": sanitized_features,
        }

    def apply_tuner_base_thresholds(
        self,
        base_thresholds: Dict[str, Any],
        *,
        feature_thresholds: Optional[Dict[str, Any]] = None,
        persist: bool = True,
        trend_thresholds: Optional[List[float]] = None,
    ) -> Dict[str, Dict[str, float]]:
        result = self.apply_tuner_thresholds(
            base_thresholds,
            feature_thresholds=feature_thresholds,
            persist=persist,
            trend_thresholds=trend_thresholds,
        )
        return result["base"]

    # --------------------------------------------------------------
    def get_tuner_base_thresholds(self) -> Dict[str, Dict[str, float]]:
        return self._get_current_base_thresholds()

    # --------------------------------------------------------------
    def _get_current_base_thresholds(self) -> Dict[str, Dict[str, float]]:
        if self._current_base_thresholds:
            return copy.deepcopy(self._current_base_thresholds)
        tuner = getattr(self.processor, "parameter_tuner", None)
        if tuner and getattr(tuner, "base_thresholds", None):
            return copy.deepcopy(tuner.base_thresholds)
        return copy.deepcopy(DEFAULT_BASE_THRESHOLDS)

    # --------------------------------------------------------------
    def get_tuner_feature_thresholds(self) -> Dict[str, Dict[str, float]]:
        return self._get_current_feature_thresholds()

    # --------------------------------------------------------------
    def _get_current_feature_thresholds(self) -> Dict[str, Dict[str, float]]:
        if self._current_feature_thresholds:
            return copy.deepcopy(self._current_feature_thresholds)
        tuner = getattr(self.processor, "parameter_tuner", None)
        if tuner and getattr(tuner, "feature_thresholds", None):
            return copy.deepcopy(tuner.feature_thresholds)
        return copy.deepcopy(DEFAULT_FEATURE_THRESHOLDS)

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

    def _sanitize_feature_thresholds(
        self, values: Optional[Dict[str, Any]]
    ) -> Dict[str, Dict[str, float]]:
        sanitized = copy.deepcopy(DEFAULT_FEATURE_THRESHOLDS)
        if not isinstance(values, dict):
            return sanitized

        def _coerce(value: Any, fallback: float) -> float:
            try:
                return float(value)
            except (TypeError, ValueError):
                return float(fallback)

        for section, defaults in DEFAULT_FEATURE_THRESHOLDS.items():
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

    def sanitize_feature_thresholds(
        self, values: Optional[Dict[str, Any]]
    ) -> Dict[str, Dict[str, float]]:
        return self._sanitize_feature_thresholds(values)


# Legacy module-level alias for compatibility with historical imports and
# pytest fixtures that monkeypatch DOM connectivity helpers.
connection_manager = _GLOBAL_CONNECTION_MANAGER

# Global service instance
_dom_service = DOMService()

def get_dom_service() -> DOMService:
    return _dom_service
