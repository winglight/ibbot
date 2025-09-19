"""Automation and orchestration utilities for DOM parameter optimisation."""

from __future__ import annotations

import copy
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

from loguru import logger

from src.dom_cache import DomCacheManager
from src.dom_parameter_tuner import DOMParameterTuner
from src.dom_service import DOMService, get_dom_service


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
        self._state: Dict[str, Any] = self._initial_state()
        self._load_state()
        self._managed_symbols: Set[str] = set()

    # ------------------------------------------------------------------
    def _initial_state(self) -> Dict[str, Any]:
        return {
            "status": "idle",
            "is_running": False,
            "last_run_at": None,
            "last_duration_ms": None,
            "last_error": None,
            "last_result": None,
            "next_run_at": None,
            "schedule": {
                "enabled": False,
                "interval_minutes": None,
                "start_at": None,
                "next_run_at": None,
            },
        }

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

    # ------------------------------------------------------------------
    @staticmethod
    def _should_manage_streams() -> bool:
        value = os.getenv("DOM_OPTIMIZER_STANDALONE", "")
        return value.strip().lower() in {"1", "true", "yes", "on"}

    # ------------------------------------------------------------------
    def ensure_data_stream(self, symbol: str, *, num_rows: int = 5) -> None:
        if not symbol or not self._should_manage_streams():
            return

        target = symbol.upper()
        if target in self._managed_symbols:
            return

        try:
            started = self.service.start(target, num_rows=num_rows)
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.warning("Failed to auto-start DOM stream", symbol=target, error=str(exc))
            return

        if started:
            logger.debug("DOM optimizer auto-started depth stream", symbol=target)
            self._managed_symbols.add(target)
        else:
            logger.warning("DOM optimizer could not start depth stream", symbol=target)

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
    ) -> Dict[str, Any]:
        with self._lock:
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
                self._persist_state_locked()
                return self._status_snapshot_locked()

            if not interval_minutes or int(interval_minutes) <= 0:
                raise ValueError("interval_minutes must be positive when enabling the schedule")

            schedule["enabled"] = True
            schedule["interval_minutes"] = int(interval_minutes)
            schedule["start_at"] = self._serialise_datetime(start_at)

            self._update_next_run_locked(reference=start_at)
            self._persist_state_locked()
            return self._status_snapshot_locked()

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
    ) -> Dict[str, Any]:
        if not symbol:
            raise ValueError("symbol is required for optimisation")

        self.ensure_data_stream(symbol)

        with self._lock:
            if self._state.get("is_running"):
                raise RuntimeError("An optimisation run is already in progress")
            self._state["is_running"] = True
            self._state["status"] = "running"
            self._state["last_error"] = None
            self._persist_state_locked()

        started_at = _now()
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
            tuner = DOMParameterTuner(base_thresholds=copy.deepcopy(base_thresholds))

            min_required = max(int(min_signals or 0), 0)
            should_optimize = evaluated > 0 and (min_required == 0 or evaluated >= min_required)

            if should_optimize:
                updated_thresholds = tuner.optimize_with_history(
                    signals,
                    obi_candidates=self._sanitize_candidates(obi_candidates),
                    ofi_candidates=self._sanitize_candidates(ofi_candidates),
                )
                applied = bool(apply_updates)
                if applied:
                    self.service.apply_tuner_base_thresholds(updated_thresholds, persist=True)
                else:
                    updated_thresholds = copy.deepcopy(updated_thresholds)
            else:
                updated_thresholds = copy.deepcopy(base_thresholds)
                applied = False

            duration_ms = ( _now() - started_at ).total_seconds() * 1000.0

            precision = positives / evaluated if evaluated else None

            window_start = metrics[0].get("timestamp") if metrics else None
            window_end = metrics[-1].get("timestamp") if metrics else None

            result = {
                "symbol": symbol,
                "applied": applied,
                "evaluated_records": evaluated,
                "positive_signals": positives,
                "negative_signals": negatives,
                "pending_signals": pending_count,
                "total_records": total_records,
                "precision": precision,
                "updated_thresholds": copy.deepcopy(updated_thresholds),
                "history_window": {"start": window_start, "end": window_end},
                "insufficient_data": not should_optimize,
            }

            with self._lock:
                self._state["status"] = "succeeded"
                self._state["is_running"] = False
                self._state["last_run_at"] = self._serialise_datetime(started_at)
                self._state["last_duration_ms"] = duration_ms
                self._state["last_error"] = None
                self._state["last_result"] = result
                self._update_next_run_locked()
                self._persist_state_locked()
            return result

        except Exception as exc:
            with self._lock:
                self._state["status"] = "failed"
                self._state["is_running"] = False
                self._state["last_run_at"] = self._serialise_datetime(started_at)
                self._state["last_duration_ms"] = duration_ms
                self._state["last_error"] = str(exc)
                self._persist_state_locked()
            logger.exception("DOM optimisation run failed")
            raise


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
