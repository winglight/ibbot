import os
import time
import asyncio
import inspect
import copy
from typing import Dict, List, Optional, Any, Callable, DefaultDict
from collections import defaultdict
from threading import Lock

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
from src.dom_parameter_tuner import (
    DOMParameterTuner,
    DEFAULT_BASE_THRESHOLDS,
    DEFAULT_FEATURE_THRESHOLDS,
)
from src.dom_signal_tracker import SignalOutcomeTracker, resolve_outcome_config
from src.dom_stats import DomStatsAggregator

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
        self._active_symbol: Optional[str] = None
        self._active_symbol_original: Optional[str] = None
        self._cache = DomCacheManager()
        self._stats = DomStatsAggregator(self._cache)
        self.db_manager = get_db_manager()
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
        self._last_start_error: Optional[str] = None
        self._last_start_error_status: int = 200
        self._start_tasks: Dict[str, asyncio.Task[bool]] = {}
        self._start_tasks_lock = Lock()

        persisted_config = self._load_persisted_config()
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
            self._stats.mark_dirty(symbol_key)

        if symbol_key:
            updates = self._outcome_tracker.evaluate_on_metrics(symbol_key, metrics)
            if updates:
                self._stats.mark_dirty(symbol_key)

        ts = time.time()
        for evt in self.processor.last_events:
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

        parameter_tuner = DOMParameterTuner(
            base_thresholds=base_thresholds,
            feature_thresholds=feature_thresholds,
            stats_window=int(tuner_params["stats_window"]),
            penalty_decay=float(tuner_params["penalty_decay"]),
            penalty_step=float(tuner_params["penalty_step"]),
            max_penalty=float(tuner_params["max_penalty"]),
            reversal_window_sec=int(tuner_params["reversal_window_sec"]),
            session_timezone=str(tuner_params["session_timezone"]),
        )

        self.processor = DOMProcessor(
            window=target_window,
            max_levels=target_levels,
            parameter_tuner=parameter_tuner,
        )
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

        if persist and getattr(self, "db_manager", None):
            payload = {
                "window": dom_config.window,
                "max_levels": dom_config.max_levels,
                "trend_thresholds": list(self.trend_thresholds),
                "tuner_base_thresholds": copy.deepcopy(base_thresholds),
                "tuner_feature_thresholds": copy.deepcopy(feature_thresholds),
                "tuner_parameters": copy.deepcopy(self._tuner_parameters),
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

    def _start_stream_blocking(
        self, symbol: str, num_rows: int = 5, *, force_restart: bool = False
    ) -> bool:
        self._last_start_error = None
        self._last_start_error_status = 200
        symbol_key = (symbol or "").upper()
        if not connection_manager.is_connected():
            logger.warning(
                "DOM depth stream start rejected - IBKR is not connected",
                symbol=symbol_key,
            )
            logger.info(
                "Connect to IBKR manually before starting DOM stream", symbol=symbol_key
            )
            reason = "IBKR connection not active"
            try:
                last_error = connection_manager.get_last_connect_error()
                if last_error:
                    reason = f"{reason}: {last_error}"
            except Exception:
                pass
            self._last_start_error = reason
            self._last_start_error_status = 400
            return False
        self._active_symbol = symbol_key
        self._active_symbol_original = symbol
        logger.info("Starting DOM depth stream", symbol=symbol_key, num_rows=num_rows)
        callback = lambda data, sym=symbol: self._on_depth_update(sym, data)
        start_kwargs: Dict[str, Any] = {"num_rows": num_rows}
        try:
            start_signature = inspect.signature(
                getattr(depth_subscriber, "start_depth_stream")
            )
        except (TypeError, ValueError):
            start_signature = None

        if start_signature is None or "force_restart" in start_signature.parameters:
            start_kwargs["force_restart"] = force_restart
        elif force_restart:
            logger.debug(
                "Depth subscriber does not support force_restart; ignoring flag",
                symbol=symbol_key,
            )

        result = depth_subscriber.start_depth_stream(
            symbol,
            callback,
            **start_kwargs,
        )
        if result:
            logger.info("DOM depth stream started", symbol=symbol_key)
            self._last_start_error = None
            self._last_start_error_status = 200
        else:
            logger.warning("Failed to start DOM depth stream", symbol=symbol_key)
            try:
                last_error, status = depth_subscriber.get_last_error()
            except Exception:
                last_error, status = None, None
            self._last_start_error = last_error or "Failed to start DOM depth stream"
            if isinstance(status, int) and 100 <= status < 600:
                self._last_start_error_status = status if status >= 400 else 500
            else:
                self._last_start_error_status = 500
            if self._active_symbol == symbol_key:
                self._active_symbol = None
                self._active_symbol_original = None
        return result

    async def start_stream(
        self, symbol: str, num_rows: int = 5, *, force_restart: bool = False
    ) -> bool:
        loop = asyncio.get_running_loop()
        self._loop = loop
        self._ensure_cleanup_task(loop)
        return await asyncio.to_thread(
            self._start_stream_blocking, symbol, num_rows, force_restart=force_restart
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
        return self._start_stream_blocking(symbol, num_rows, force_restart=force_restart)

    def get_last_start_error(self) -> tuple[Optional[str], int]:
        return self._last_start_error, self._last_start_error_status

    def _stop_stream_blocking(self, symbol: str) -> bool:
        symbol_key = (symbol or "").upper()
        if self._active_symbol and symbol_key == self._active_symbol:
            self._active_symbol = None
            self._active_symbol_original = None
        result = depth_subscriber.stop_depth_stream(symbol)
        if result:
            logger.info("Stopped DOM depth stream", symbol=symbol_key)
        else:
            logger.warning("No active DOM depth stream to stop", symbol=symbol_key)
        return result

    async def stop_stream(self, symbol: str) -> bool:
        symbol_key = (symbol or "").upper()
        had_pending = False
        with self._start_tasks_lock:
            task = self._start_tasks.get(symbol_key)
            if task and not task.done():
                task.cancel()
                had_pending = True
            self._start_tasks.pop(symbol_key, None)

        if self._active_symbol and symbol_key == self._active_symbol:
            self._active_symbol = None
            self._active_symbol_original = None

        if not self._loop or self._loop.is_closed():
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                pass
        result = await asyncio.to_thread(self._stop_stream_blocking, symbol)
        return bool(result or had_pending)

    def stop(self, symbol: str) -> bool:
        return self._stop_stream_blocking(symbol)

    def is_start_in_progress(self, symbol: Optional[str] = None) -> bool:
        symbol_key = (symbol or "").upper()
        with self._start_tasks_lock:
            if symbol_key:
                task = self._start_tasks.get(symbol_key)
                return bool(task and not task.done())
            return any(task and not task.done() for task in self._start_tasks.values())

    def is_stream_active(self, symbol: Optional[str] = None) -> bool:
        candidate = symbol or self._active_symbol_original
        if candidate:
            try:
                if depth_subscriber.is_depth_stream_active(candidate):
                    return True
            except Exception:
                pass

        symbol_key = (symbol or "").upper()
        if not symbol_key and self._active_symbol:
            symbol_key = self._active_symbol

        return bool(symbol_key and self._active_symbol == symbol_key)

    def _on_start_task_done(self, symbol: str, fut: "asyncio.Future[bool]") -> None:
        with self._start_tasks_lock:
            current = self._start_tasks.get(symbol)
            if current is fut:
                self._start_tasks.pop(symbol, None)
        try:
            result = fut.result()
        except asyncio.CancelledError:
            if self._active_symbol == symbol:
                self._active_symbol = None
                self._active_symbol_original = None
            return
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.error(
                "DOM depth stream start task failed", symbol=symbol, error=str(exc)
            )
            self._last_start_error = str(exc) or "Failed to start DOM depth stream"
            self._last_start_error_status = 500
            if self._active_symbol == symbol:
                self._active_symbol = None
                self._active_symbol_original = None
        else:
            if not result and self._active_symbol == symbol:
                self._active_symbol = None
                self._active_symbol_original = None

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
        tuner = getattr(self.processor, "parameter_tuner", None)
        if tuner and getattr(tuner, "base_thresholds", None):
            return copy.deepcopy(tuner.base_thresholds)
        return copy.deepcopy(DEFAULT_BASE_THRESHOLDS)

    # --------------------------------------------------------------
    def get_tuner_feature_thresholds(self) -> Dict[str, Dict[str, float]]:
        return self._get_current_feature_thresholds()

    # --------------------------------------------------------------
    def _get_current_feature_thresholds(self) -> Dict[str, Dict[str, float]]:
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


# Global service instance
_dom_service = DOMService()

def get_dom_service() -> DOMService:
    return _dom_service
