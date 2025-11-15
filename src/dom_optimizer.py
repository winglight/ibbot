"""Automation and orchestration utilities for DOM parameter optimisation."""

from __future__ import annotations

import copy
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from loguru import logger

from src.dom_cache import DomCacheManager
from src.dom_parameter_tuner import DOMParameterTuner
from src.dom_service import DOMService, get_dom_service
from src.dom_settings import get_automation_defaults


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_timezone(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class DOMOptimizationOrchestrator:
    """Coordinate optimisation runs and automation schedule."""

    STATE_CONFIG_KEY = "dom_automation_state"

    def __init__(
        self,
        service: Optional[DOMService] = None,
        *,
        cache: Optional[DomCacheManager] = None,
        db_manager: Optional[Any] = None,
    ) -> None:
        self.service = service or get_dom_service()
        self._cache = cache
        self.db_manager = db_manager or getattr(self.service, "db_manager", None)
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._scheduler_thread: Optional[threading.Thread] = None
        self._state: Dict[str, Any] = self._initial_state()
        self._load_state()
        self._start_scheduler()
        logger.info(
            "DOM optimisation orchestrator initialised",
            schedule=self._state.get("schedule"),
            plan=self._state.get("plan"),
        )

    # ------------------------------------------------------------------
    def _initial_state(self) -> Dict[str, Any]:
        defaults = get_automation_defaults()
        return {
            "status": "idle",
            "is_running": False,
            "last_started_at": None,
            "last_run_at": None,
            "last_duration_ms": None,
            "last_error": None,
            "last_result": None,
            "scheduled_at": None,
            "next_run_at": None,
            "schedule": {
                "enabled": False,
                "interval_minutes": defaults.interval_minutes,
                "start_at": None,
                "next_run_at": None,
            },
            "plan": {
                "symbol": defaults.symbol,
                "lookback_minutes": defaults.lookback_minutes,
                "limit": defaults.limit,
                "apply_updates": defaults.apply_updates,
                "min_signals": defaults.min_signals,
                "obi_candidates": None,
                "ofi_candidates": None,
                "feature_thresholds": None,
            },
        }

    # ------------------------------------------------------------------
    def _start_scheduler(self) -> None:
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            return

        self._scheduler_thread = threading.Thread(
            target=self._scheduler_loop,
            name="DOMOptimizationScheduler",
            daemon=True,
        )
        self._scheduler_thread.start()
        self._wake_event.set()

    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            self._scheduler_thread.join(timeout=1.0)

    # ------------------------------------------------------------------
    def _resolve_cache(self) -> DomCacheManager:
        cache = self._cache or getattr(self.service, "_cache", None)
        if cache is None:
            raise RuntimeError("DOM cache manager is not available")
        return cache

    # ------------------------------------------------------------------
    def _status_snapshot_locked(self) -> Dict[str, Any]:
        return copy.deepcopy(self._state)

    # ------------------------------------------------------------------
    @staticmethod
    def _serialise_datetime(value: Optional[datetime]) -> Optional[str]:
        if value is None:
            return None
        value = _ensure_timezone(value)
        return value.isoformat()

    # ------------------------------------------------------------------
    @staticmethod
    def _deserialise_datetime(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None
        return _ensure_timezone(parsed)

    # ------------------------------------------------------------------
    def _load_state(self) -> None:
        if not self.db_manager:
            return
        try:
            payload = self.db_manager.get_config(self.STATE_CONFIG_KEY, None)
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.warning("Failed to load DOM automation state: {}", exc)
            return
        if isinstance(payload, dict) and payload:
            with self._lock:
                self._state.update(payload)
                self._ensure_state_defaults_locked()
            logger.info(
                "Loaded persisted DOM automation state",
                schedule=payload.get("schedule"),
                plan=payload.get("plan"),
            )

    # ------------------------------------------------------------------
    def _ensure_state_defaults_locked(self) -> None:
        defaults = self._initial_state()
        schedule_defaults = defaults.get("schedule", {})
        plan_defaults = defaults.get("plan", {})

        schedule = self._state.setdefault("schedule", {})
        for key, value in schedule_defaults.items():
            schedule.setdefault(key, copy.deepcopy(value))

        plan = self._state.setdefault("plan", {})
        for key, value in plan_defaults.items():
            plan.setdefault(key, copy.deepcopy(value))

    # ------------------------------------------------------------------
    def _persist_state_locked(self) -> None:
        if not self.db_manager:
            return
        try:
            self.db_manager.save_config(
                self.STATE_CONFIG_KEY,
                self._state,
                "DOM optimisation automation state",
            )
            logger.debug(
                "Persisted DOM automation state",
                schedule=self._state.get("schedule"),
                next_run=self._state.get("next_run_at"),
            )
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.warning("Failed to persist DOM automation state: {}", exc)

    # ------------------------------------------------------------------
    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            return self._status_snapshot_locked()

    # ------------------------------------------------------------------
    def reset_state(self) -> None:
        with self._lock:
            self._state = self._initial_state()
            self._persist_state_locked()
        self._wake_event.set()
        logger.info(
            "DOM automation state reset",
            schedule=self._state.get("schedule"),
            plan=self._state.get("plan"),
        )

    # ------------------------------------------------------------------
    def _update_next_run_locked(self, reference: Optional[datetime] = None) -> None:
        schedule = self._state.get("schedule", {})
        if not schedule.get("enabled"):
            self._state["next_run_at"] = None
            schedule["next_run_at"] = None
            return

        interval = schedule.get("interval_minutes")
        if not isinstance(interval, int) or interval <= 0:
            self._state["next_run_at"] = None
            schedule["next_run_at"] = None
            return

        reference = _ensure_timezone(reference)
        last_run = self._deserialise_datetime(self._state.get("last_run_at"))
        start_at = self._deserialise_datetime(schedule.get("start_at"))

        if reference is not None:
            base_time = reference
        elif start_at and start_at > _now():
            base_time = start_at
        elif last_run:
            base_time = last_run + timedelta(minutes=interval)
        else:
            base_time = _now() + timedelta(minutes=interval)

        next_run = self._serialise_datetime(base_time)
        self._state["next_run_at"] = next_run
        schedule["next_run_at"] = next_run

    # ------------------------------------------------------------------
    def configure_schedule(
        self,
        *,
        enabled: bool,
        interval_minutes: Optional[int],
        start_at: Optional[datetime] = None,
        symbol: Optional[str] = None,
        lookback_minutes: Optional[int] = None,
        limit: Optional[int] = None,
        apply_updates: Optional[bool] = None,
        min_signals: Optional[int] = None,
        obi_candidates: Optional[List[Any]] = None,
        ofi_candidates: Optional[List[Any]] = None,
        feature_thresholds: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            self._ensure_state_defaults_locked()
            plan = self._state.setdefault("plan", {})

            if symbol is not None:
                cleaned = symbol.strip().upper() if symbol else None
                plan["symbol"] = cleaned or None

            if lookback_minutes is not None:
                try:
                    minutes = int(lookback_minutes)
                except (TypeError, ValueError):
                    minutes = 0
                plan["lookback_minutes"] = minutes if minutes > 0 else None

            if limit is not None:
                try:
                    parsed_limit = int(limit)
                except (TypeError, ValueError):
                    parsed_limit = 0
                plan["limit"] = parsed_limit if parsed_limit > 0 else None

            if apply_updates is not None:
                plan["apply_updates"] = bool(apply_updates)

            if min_signals is not None:
                try:
                    parsed_min = int(min_signals)
                except (TypeError, ValueError):
                    parsed_min = 0
                plan["min_signals"] = max(parsed_min, 0)

            if obi_candidates is not None:
                plan["obi_candidates"] = self._sanitize_candidates(obi_candidates)

            if ofi_candidates is not None:
                plan["ofi_candidates"] = self._sanitize_candidates(ofi_candidates)

            if feature_thresholds is not None:
                plan["feature_thresholds"] = self.service.sanitize_feature_thresholds(feature_thresholds)

            schedule = self._state.setdefault("schedule", {})
            if not enabled:
                schedule.update(
                    {
                        "enabled": False,
                        "interval_minutes": None,
                        "start_at": None,
                        "next_run_at": None,
                    }
                )
                self._state["next_run_at"] = None
                self._state["scheduled_at"] = None
                self._state["is_running"] = False
                self._state["status"] = "idle"
                self._persist_state_locked()
                status = self._status_snapshot_locked()
            else:
                if not interval_minutes or int(interval_minutes) <= 0:
                    raise ValueError(
                        "interval_minutes must be positive when enabling the schedule"
                    )

                schedule["enabled"] = True
                schedule["interval_minutes"] = int(interval_minutes)
                schedule["start_at"] = self._serialise_datetime(start_at)

                if not self._state.get("is_running"):
                    self._state["status"] = "idle"
                self._state["scheduled_at"] = None
                self._update_next_run_locked(reference=start_at)
                self._persist_state_locked()
                status = self._status_snapshot_locked()

        self._wake_event.set()
        if status.get("schedule", {}).get("enabled"):
            logger.info(
                "DOM automation schedule configured",
                plan=status.get("plan"),
                next_run=status.get("next_run_at"),
            )
        else:
            logger.info(
                "DOM automation schedule disabled",
                plan=status.get("plan"),
            )
        return status

    # ------------------------------------------------------------------
    def _prepare_history(
        self,
        symbol: str,
        *,
        start_time: Optional[Any] = None,
        end_time: Optional[Any] = None,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        cache = self._resolve_cache()
        history = cache.get_history(
            symbol,
            limit=limit,
            start_time=start_time,
            end_time=end_time,
            include_snapshots=False,
        )
        return history

    # ------------------------------------------------------------------
    def _sanitize_candidates(self, values: Optional[List[Any]]) -> Optional[List[float]]:
        if not values:
            return None
        sanitized: List[float] = []
        for item in values:
            try:
                sanitized.append(float(item))
            except (TypeError, ValueError):
                continue
        return sanitized or None

    # ------------------------------------------------------------------
    def run_optimization(
        self,
        symbol: str,
        *,
        start_time: Optional[Any] = None,
        end_time: Optional[Any] = None,
        limit: Optional[int] = None,
        apply_updates: bool = True,
        min_signals: int = 0,
        obi_candidates: Optional[List[Any]] = None,
        ofi_candidates: Optional[List[Any]] = None,
        trend_candidates: Optional[List[Any]] = None,
        feature_thresholds: Optional[Dict[str, Any]] = None,
        scheduled: bool = False,
        scheduled_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        if not symbol:
            raise ValueError("symbol is required for optimisation")

        started_at = _now()
        trend_thresholds: Optional[List[float]] = None
        if trend_candidates:
            try:
                trend_thresholds = self.service._sanitize_thresholds(list(trend_candidates))  # type: ignore[attr-defined]
            except Exception:
                trend_thresholds = None
        with self._lock:
            if self._state.get("is_running"):
                raise RuntimeError("An optimisation run is already in progress")
            self._state["is_running"] = True
            self._state["status"] = "running"
            self._state["last_error"] = None
            self._state["scheduled_at"] = (
                self._serialise_datetime(scheduled_time) if scheduled else None
            )
            self._state["last_started_at"] = self._serialise_datetime(started_at)
            self._persist_state_locked()

        logger.info(
            "Starting DOM optimisation run",
            symbol=symbol,
            scheduled=scheduled,
            scheduled_time=self._serialise_datetime(scheduled_time) if scheduled_time else None,
        )
        duration_ms: Optional[float] = None
        result: Dict[str, Any] = {}

        try:
            history = self._prepare_history(
                symbol,
                start_time=start_time,
                end_time=end_time,
                limit=limit,
            )
            metrics = history.get("metrics", []) if isinstance(history, dict) else []
            total_records = len(metrics)

            signals = [
                item
                for item in metrics
                if isinstance(item, dict)
                and item.get("signal_type") in {"long", "short"}
                and item.get("outcome") is not None
            ]
            pending_count = sum(
                1
                for item in metrics
                if isinstance(item, dict)
                and item.get("signal_type") in {"long", "short"}
                and item.get("outcome") is None
            )

            positives = sum(1 for item in signals if item.get("outcome"))
            negatives = sum(1 for item in signals if item.get("outcome") == 0)
            evaluated = positives + negatives

            base_thresholds = self.service.get_tuner_base_thresholds()
            current_feature_thresholds = self.service.get_tuner_feature_thresholds()
            requested_feature_thresholds = (
                self.service.sanitize_feature_thresholds(feature_thresholds)
                if feature_thresholds is not None
                else copy.deepcopy(current_feature_thresholds)
            )
            tuner = DOMParameterTuner(
                base_thresholds=copy.deepcopy(base_thresholds),
                feature_thresholds=copy.deepcopy(requested_feature_thresholds),
            )

            min_required = max(int(min_signals or 0), 0)
            should_optimize = evaluated > 0 and (min_required == 0 or evaluated >= min_required)

            if should_optimize:
                updated_base_thresholds = tuner.optimize_with_history(
                    signals,
                    obi_candidates=self._sanitize_candidates(obi_candidates),
                    ofi_candidates=self._sanitize_candidates(ofi_candidates),
                )
                updated_thresholds = {
                    "base": copy.deepcopy(updated_base_thresholds),
                    "features": copy.deepcopy(requested_feature_thresholds),
                }
                applied = bool(apply_updates)
                if applied:
                    self.service.apply_tuner_base_thresholds(
                        updated_base_thresholds,
                        persist=True,
                        trend_thresholds=trend_thresholds,
                        feature_thresholds=requested_feature_thresholds,
                    )
                else:
                    updated_thresholds = {
                        "base": copy.deepcopy(updated_base_thresholds),
                        "features": copy.deepcopy(requested_feature_thresholds),
                    }
            else:
                updated_thresholds = {
                    "base": copy.deepcopy(base_thresholds),
                    "features": copy.deepcopy(requested_feature_thresholds),
                }
                applied = False
                logger.info(
                    "Skipping DOM optimisation due to insufficient evaluated signals",
                    symbol=symbol,
                    evaluated=evaluated,
                    min_required=min_required,
                    pending=pending_count,
                )

            finished_at = _now()
            duration_ms = (finished_at - started_at).total_seconds() * 1000.0

            precision = positives / evaluated if evaluated else None

            window_start = metrics[0].get("timestamp") if metrics else None
            window_end = metrics[-1].get("timestamp") if metrics else None

            result = {
                "symbol": symbol,
                "applied": applied,
                "started_at": self._serialise_datetime(started_at),
                "completed_at": self._serialise_datetime(finished_at),
                "duration_ms": duration_ms,
                "evaluated_records": evaluated,
                "positive_signals": positives,
                "negative_signals": negatives,
                "pending_signals": pending_count,
                "total_records": total_records,
                "precision": precision,
                "updated_thresholds": copy.deepcopy(updated_thresholds.get("base")),
                "feature_thresholds": copy.deepcopy(updated_thresholds.get("features")),
                "history_window": {"start": window_start, "end": window_end},
                "insufficient_data": not should_optimize,
            }
            if trend_thresholds:
                result["trend_thresholds"] = list(trend_thresholds)

            logger.info(
                "DOM optimisation run completed",
                symbol=symbol,
                evaluated=evaluated,
                applied=applied,
                precision=precision,
                duration_ms=duration_ms,
                pending=pending_count,
            )

            with self._lock:
                self._state["status"] = "succeeded"
                self._state["is_running"] = False
                self._state["last_run_at"] = self._serialise_datetime(finished_at)
                self._state["last_duration_ms"] = duration_ms
                self._state["last_error"] = None
                self._state["last_result"] = result
                self._update_next_run_locked()
                self._persist_state_locked()
            return result

        except Exception as exc:
            finished_at = _now()
            duration_ms = (finished_at - started_at).total_seconds() * 1000.0
            with self._lock:
                self._state["status"] = "failed"
                self._state["is_running"] = False
                self._state["last_run_at"] = self._serialise_datetime(finished_at)
                self._state["last_started_at"] = self._serialise_datetime(started_at)
                self._state["last_duration_ms"] = duration_ms
                self._state["last_error"] = str(exc)
                self._update_next_run_locked(reference=_now())
                self._persist_state_locked()
            logger.exception("DOM optimisation run failed")
            raise

    # ------------------------------------------------------------------
    def _compute_sleep_duration(self) -> float:
        with self._lock:
            enabled = bool(self._state.get("schedule", {}).get("enabled"))
            next_run = self._deserialise_datetime(self._state.get("next_run_at"))

        if not enabled or next_run is None:
            return 60.0

        delta = (next_run - _now()).total_seconds()
        if delta <= 0:
            return 0.0
        return max(1.0, min(delta, 300.0))

    # ------------------------------------------------------------------
    def _execute_plan(
        self,
        plan: Dict[str, Any],
        *,
        scheduled_time: Optional[datetime] = None,
    ) -> Optional[Dict[str, Any]]:
        symbol_raw = plan.get("symbol")
        symbol = symbol_raw.strip().upper() if isinstance(symbol_raw, str) else None
        reference_time = _ensure_timezone(scheduled_time) or _now()

        if not symbol:
            with self._lock:
                self._state["status"] = "idle"
                self._state["scheduled_at"] = self._serialise_datetime(reference_time)
                self._update_next_run_locked(reference=_now())
                self._persist_state_locked()
            logger.debug("Skipping scheduled DOM optimisation due to missing symbol")
            return None

        lookback = plan.get("lookback_minutes")
        try:
            lookback_minutes = int(lookback) if lookback is not None else None
        except (TypeError, ValueError):
            lookback_minutes = None
        if lookback_minutes is not None and lookback_minutes <= 0:
            lookback_minutes = None

        if lookback_minutes:
            start_time = (reference_time - timedelta(minutes=lookback_minutes)).timestamp() * 1000.0
        else:
            start_time = None

        limit_value = plan.get("limit")
        try:
            limit_int = int(limit_value) if limit_value is not None else None
        except (TypeError, ValueError):
            limit_int = None
        if limit_int is not None and limit_int <= 0:
            limit_int = None

        min_required = plan.get("min_signals", 0)
        try:
            min_required_int = int(min_required)
        except (TypeError, ValueError):
            min_required_int = 0
        min_required_int = max(min_required_int, 0)

        apply_updates = bool(plan.get("apply_updates", True))

        obi_candidates = self._sanitize_candidates(plan.get("obi_candidates"))
        ofi_candidates = self._sanitize_candidates(plan.get("ofi_candidates"))
        feature_thresholds = plan.get("feature_thresholds")

        return self.run_optimization(
            symbol,
            start_time=start_time,
            end_time=None,
            limit=limit_int,
            apply_updates=apply_updates,
            min_signals=min_required_int,
            obi_candidates=obi_candidates,
            ofi_candidates=ofi_candidates,
            feature_thresholds=feature_thresholds,
            scheduled=True,
            scheduled_time=reference_time,
        )

    # ------------------------------------------------------------------
    def run_scheduled_once(
        self, *, reference_time: Optional[datetime] = None
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            enabled = bool(self._state.get("schedule", {}).get("enabled"))
            next_run_iso = self._state.get("next_run_at")
            plan = copy.deepcopy(self._state.get("plan", {}))

        if not enabled:
            return None

        now = _ensure_timezone(reference_time) or _now()
        next_run = self._deserialise_datetime(next_run_iso)

        if next_run and next_run > now:
            return None

        scheduled_time = next_run or now
        try:
            result = self._execute_plan(plan, scheduled_time=scheduled_time)
        except RuntimeError as exc:
            logger.info(
                "Scheduled DOM optimisation already running",
                error=str(exc),
            )
            deadline = time.time() + 3.0
            while time.time() < deadline:
                status = self.get_status()
                if not status.get("is_running"):
                    last_result = status.get("last_result")
                    if last_result:
                        logger.info(
                            "Returning last automation result after concurrent run",
                            symbol=last_result.get("symbol"),
                        )
                        return copy.deepcopy(last_result)
                    break
                time.sleep(0.05)
            return None
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.exception("Scheduled DOM optimisation execution failed")
            raise
        if result:
            logger.info(
                "Executed scheduled DOM optimisation",
                symbol=result.get("symbol"),
                scheduled_time=self._serialise_datetime(scheduled_time) if scheduled_time else None,
                next_run=self.get_status().get("next_run_at"),
            )
        return result

    # ------------------------------------------------------------------
    def _scheduler_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_scheduled_once()
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.warning("DOM optimisation scheduler error: {}", exc)

            sleep_seconds = self._compute_sleep_duration()
            if sleep_seconds <= 0:
                sleep_seconds = 1.0

            triggered = self._wake_event.wait(timeout=sleep_seconds)
            if triggered:
                self._wake_event.clear()


_optimizer_instance: Optional[DOMOptimizationOrchestrator] = None


def get_dom_optimization_orchestrator() -> DOMOptimizationOrchestrator:
    global _optimizer_instance
    if _optimizer_instance is None:
        _optimizer_instance = DOMOptimizationOrchestrator()
    return _optimizer_instance


__all__ = [
    "DOMOptimizationOrchestrator",
    "get_dom_optimization_orchestrator",
]
