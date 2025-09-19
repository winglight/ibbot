"""DOM statistics aggregation and query utilities."""

from __future__ import annotations

import os
import pickle
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
from loguru import logger

from src.dom_cache import DomCacheManager
from src.utils import parse_local_datetime


def _ensure_directory(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _sanitize_symbol(symbol: str) -> str:
    if not symbol:
        return "UNKNOWN"
    import re as _re

    safe = _re.sub(r"[^A-Za-z0-9._-]", "_", symbol.upper())
    return safe or "UNKNOWN"


def _to_iso(ms: Optional[float]) -> Optional[str]:
    if ms is None or not np.isfinite(ms):
        return None
    try:
        seconds = float(ms) / 1000.0
    except (TypeError, ValueError):
        return None
    dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return dt.isoformat()


def _parse_timestamp_ms(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except (TypeError, ValueError):
            dt = parse_local_datetime(text)
            if dt is None:
                return None
            return dt.timestamp() * 1000.0
    return None


def _extract_timestamp_ms(payload: Dict[str, Any]) -> Optional[float]:
    for key in ("timestamp", "timestamp_ms", "timestampMs", "ts", "time"):
        if key in payload:
            candidate = _parse_timestamp_ms(payload.get(key))
            if candidate is not None:
                return candidate
    if "timestamp_ns" in payload:
        candidate = payload.get("timestamp_ns")
        if isinstance(candidate, (int, float)):
            return float(candidate) / 1_000_000.0
    if "timestampNs" in payload:
        candidate = payload.get("timestampNs")
        if isinstance(candidate, (int, float)):
            return float(candidate) / 1_000_000.0
    if "ts_ns" in payload:
        candidate = payload.get("ts_ns")
        if isinstance(candidate, (int, float)):
            return float(candidate) / 1_000_000.0
    return None


@dataclass
class DomStatsPayload:
    symbol: str
    timestamps: np.ndarray
    cum_total: np.ndarray
    cum_long: np.ndarray
    cum_short: np.ndarray
    cum_success: np.ndarray
    cum_failure: np.ndarray
    cum_delay_sum: np.ndarray
    cum_delay_count: np.ndarray
    metrics_tail: List[Dict[str, Any]]
    metrics_offset: int
    source_count: int
    rebuilt_at: float
    max_timestamp: Optional[float]

    @property
    def is_empty(self) -> bool:
        return self.source_count == 0 or self.timestamps.size == 0


class DomStatsAggregator:
    """Aggregate DOM metrics into rolling statistics and chart-friendly slices."""

    VERSION = 1

    def __init__(
        self,
        cache_manager: DomCacheManager,
        *,
        base_dir: str = "data",
        flush_interval: float = 5.0,
        max_points: int = 3000,
        max_cached_metrics: int = 50_000,
        default_lookback_seconds: int = 1800,
    ) -> None:
        self.cache_manager = cache_manager
        self.base_dir = os.path.join(base_dir, "dom_stats")
        self.flush_interval = max(float(flush_interval), 1.0)
        self.max_points = max(int(max_points), 100)
        self.max_cached_metrics = max(int(max_cached_metrics), self.max_points)
        self.default_lookback_seconds = max(int(default_lookback_seconds), 60)

        _ensure_directory(self.base_dir)

        self._lock = threading.Lock()
        self._payloads: Dict[str, DomStatsPayload] = {}
        self._dirty: set[str] = set()
        self._stop_event = threading.Event()
        self._worker = threading.Thread(
            target=self._run_worker,
            name="DomStatsAggregator",
            daemon=True,
        )
        self._worker.start()

    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        self._stop_event.set()
        self._worker.join(timeout=1.0)

    # ------------------------------------------------------------------
    def mark_dirty(self, symbol: str) -> None:
        safe = _sanitize_symbol(symbol)
        with self._lock:
            self._dirty.add(safe)

    # ------------------------------------------------------------------
    def get_stats(
        self,
        symbol: str,
        *,
        lookback_seconds: Optional[int] = None,
        start: Optional[Any] = None,
        end: Optional[Any] = None,
        max_points: Optional[int] = None,
    ) -> Dict[str, Any]:
        safe = _sanitize_symbol(symbol)
        payload = self._ensure_payload(safe)

        if payload is None or payload.is_empty:
            return self._empty_response(symbol)

        timestamps = payload.timestamps
        max_ts = payload.max_timestamp if payload.max_timestamp is not None else float(timestamps[-1])

        requested_end = _parse_timestamp_ms(end) if end is not None else None
        if requested_end is None:
            requested_end = max_ts

        requested_start = _parse_timestamp_ms(start) if start is not None else None

        lookback = None
        if lookback_seconds is not None and lookback_seconds > 0:
            lookback = float(lookback_seconds)
        elif requested_start is None:
            lookback = float(self.default_lookback_seconds)

        if lookback is not None and requested_start is None:
            requested_start = requested_end - lookback * 1000.0

        if requested_start is None:
            requested_start = float(timestamps[0])

        start_ms = max(float(timestamps[0]), requested_start)
        end_ms = min(float(timestamps[-1]), requested_end)

        if end_ms < start_ms:
            start_ms, end_ms = float(timestamps[0]), float(timestamps[-1])

        start_idx = int(np.searchsorted(timestamps, start_ms, side="left"))
        end_idx = int(np.searchsorted(timestamps, end_ms, side="right") - 1)

        if end_idx < 0 or start_idx >= timestamps.size:
            return self._empty_response(
                symbol,
                window_start=start_ms,
                window_end=end_ms,
                lookback_seconds=lookback_seconds,
                metadata=self._metadata(payload),
            )

        start_idx = max(0, start_idx)
        end_idx = min(end_idx, timestamps.size - 1)

        stats = self._range_stats(payload, start_idx, end_idx)
        chart_metrics = self._range_metrics(payload, start_idx, end_idx, max_points)
        latest_signal = self._latest_signal(chart_metrics)

        response = {
            "success": True,
            "symbol": symbol,
            "stats": stats,
            "latest_signal": latest_signal,
            "chart_metrics": chart_metrics,
            "window": {
                "start": _to_iso(float(timestamps[start_idx])),
                "end": _to_iso(float(timestamps[end_idx])),
                "lookback_seconds": lookback_seconds if (lookback_seconds or 0) > 0 else None,
            },
            "metadata": self._metadata(payload),
        }
        return response

    # ------------------------------------------------------------------
    def _metadata(self, payload: DomStatsPayload) -> Dict[str, Any]:
        return {
            "version": self.VERSION,
            "rebuilt_at": _to_iso(payload.rebuilt_at * 1000.0) if payload.rebuilt_at else None,
            "source_records": payload.source_count,
            "cached_metrics": len(payload.metrics_tail),
            "metrics_offset": payload.metrics_offset,
            "max_timestamp": _to_iso(payload.max_timestamp) if payload.max_timestamp else None,
        }

    # ------------------------------------------------------------------
    def _empty_response(
        self,
        symbol: str,
        *,
        window_start: Optional[float] = None,
        window_end: Optional[float] = None,
        lookback_seconds: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
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
                "start": _to_iso(window_start) if window_start else None,
                "end": _to_iso(window_end) if window_end else None,
                "lookback_seconds": lookback_seconds if (lookback_seconds or 0) > 0 else None,
            },
            "metadata": metadata or {
                "version": self.VERSION,
                "rebuilt_at": None,
                "source_records": 0,
                "cached_metrics": 0,
                "metrics_offset": 0,
                "max_timestamp": None,
            },
        }

    # ------------------------------------------------------------------
    def _range_stats(
        self,
        payload: DomStatsPayload,
        start_idx: int,
        end_idx: int,
    ) -> Dict[str, Any]:
        if end_idx < start_idx:
            return {
                "total": 0,
                "long": 0,
                "short": 0,
                "evaluated": 0,
                "success": 0,
                "failure": 0,
                "pending": 0,
                "precision": None,
                "avg_delay_seconds": None,
            }

        def _diff(arr: np.ndarray) -> float:
            if arr.size == 0:
                return 0.0
            end_value = float(arr[end_idx])
            start_value = float(arr[start_idx - 1]) if start_idx > 0 else 0.0
            return end_value - start_value

        total = int(round(_diff(payload.cum_total)))
        long_count = int(round(_diff(payload.cum_long)))
        short_count = int(round(_diff(payload.cum_short)))
        success = int(round(_diff(payload.cum_success)))
        failure = int(round(_diff(payload.cum_failure)))
        evaluated = success + failure
        pending = max(total - evaluated, 0)

        delay_sum = _diff(payload.cum_delay_sum)
        delay_count = _diff(payload.cum_delay_count)
        avg_delay = None
        if delay_count > 0:
            avg_delay = delay_sum / delay_count

        precision = None
        if evaluated > 0:
            precision = success / evaluated

        return {
            "total": total,
            "long": long_count,
            "short": short_count,
            "evaluated": evaluated,
            "success": success,
            "failure": failure,
            "pending": pending,
            "precision": precision,
            "avg_delay_seconds": avg_delay,
        }

    # ------------------------------------------------------------------
    def _range_metrics(
        self,
        payload: DomStatsPayload,
        start_idx: int,
        end_idx: int,
        max_points: Optional[int],
    ) -> List[Dict[str, Any]]:
        metrics = payload.metrics_tail
        if not metrics:
            return []

        offset = payload.metrics_offset
        start_index = max(start_idx, offset)
        end_index = min(end_idx, offset + len(metrics) - 1)
        if start_index > end_index:
            return []

        rel_start = start_index - offset
        rel_end = end_index - offset
        window_metrics = metrics[rel_start : rel_end + 1]

        if not window_metrics:
            return []

        capped = int(max_points or self.max_points)
        c = max(capped, 1)
        if len(window_metrics) > c:
            idx = np.linspace(0, len(window_metrics) - 1, num=c, dtype=int)
            selected = [window_metrics[i] for i in idx]
        else:
            selected = window_metrics

        # Ensure we do not leak shared references back to callers
        return [dict(item) for item in selected]

    # ------------------------------------------------------------------
    def _latest_signal(self, metrics: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        for item in reversed(list(metrics)):
            sig_type = item.get("signal_type")
            if sig_type in {"long", "short"}:
                return dict(item)
        return None

    # ------------------------------------------------------------------
    def _ensure_payload(self, safe_symbol: str) -> Optional[DomStatsPayload]:
        with self._lock:
            payload = self._payloads.get(safe_symbol)
            if payload is not None and safe_symbol not in self._dirty:
                return payload

        if safe_symbol in self._dirty:
            # Perform a synchronous rebuild to avoid stale reads
            try:
                self._rebuild_symbol(safe_symbol)
            except Exception:
                logger.exception("Failed to rebuild DOM stats for %s", safe_symbol)

        with self._lock:
            payload = self._payloads.get(safe_symbol)
            if payload is not None:
                return payload

        # Attempt to load from disk if not in memory
        payload = self._load_payload(safe_symbol)
        if payload is not None:
            with self._lock:
                self._payloads[safe_symbol] = payload
        return payload

    # ------------------------------------------------------------------
    def _run_worker(self) -> None:
        while not self._stop_event.is_set():
            triggered = self._stop_event.wait(self.flush_interval)
            if triggered and self._stop_event.is_set():
                break

            with self._lock:
                symbols = list(self._dirty)
                self._dirty.clear()

            for safe_symbol in symbols:
                try:
                    self._rebuild_symbol(safe_symbol)
                except Exception:
                    logger.exception("Failed to rebuild DOM stats for %s", safe_symbol)

    # ------------------------------------------------------------------
    def _rebuild_symbol(self, safe_symbol: str) -> None:
        raw_metrics = self.cache_manager.get_history(
            safe_symbol,
            limit=None,
            include_snapshots=False,
        ).get("metrics") or []

        if not raw_metrics:
            payload = DomStatsPayload(
                symbol=safe_symbol,
                timestamps=np.array([], dtype=np.float64),
                cum_total=np.array([], dtype=np.float64),
                cum_long=np.array([], dtype=np.float64),
                cum_short=np.array([], dtype=np.float64),
                cum_success=np.array([], dtype=np.float64),
                cum_failure=np.array([], dtype=np.float64),
                cum_delay_sum=np.array([], dtype=np.float64),
                cum_delay_count=np.array([], dtype=np.float64),
                metrics_tail=[],
                metrics_offset=0,
                source_count=0,
                rebuilt_at=time.time(),
                max_timestamp=None,
            )
            self._store_payload(safe_symbol, payload)
            return

        raw_timestamps = np.array([
            _extract_timestamp_ms(item) or np.nan for item in raw_metrics
        ], dtype=np.float64)

        order = np.argsort(raw_timestamps, kind="mergesort")
        sorted_ts = raw_timestamps[order]
        metrics_sorted_all = [raw_metrics[i] for i in order]

        clean_pairs: List[Tuple[float, Dict[str, Any]]] = []
        for ts_value, metric in zip(sorted_ts, metrics_sorted_all):
            if not np.isfinite(ts_value) or ts_value <= 0:
                continue
            clean_pairs.append((float(ts_value), metric))

        if not clean_pairs:
            payload = DomStatsPayload(
                symbol=safe_symbol,
                timestamps=np.array([], dtype=np.float64),
                cum_total=np.array([], dtype=np.float64),
                cum_long=np.array([], dtype=np.float64),
                cum_short=np.array([], dtype=np.float64),
                cum_success=np.array([], dtype=np.float64),
                cum_failure=np.array([], dtype=np.float64),
                cum_delay_sum=np.array([], dtype=np.float64),
                cum_delay_count=np.array([], dtype=np.float64),
                metrics_tail=[],
                metrics_offset=0,
                source_count=0,
                rebuilt_at=time.time(),
                max_timestamp=None,
            )
            self._store_payload(safe_symbol, payload)
            return

        timestamps = np.array([pair[0] for pair in clean_pairs], dtype=np.float64)
        metrics_sorted = [pair[1] for pair in clean_pairs]

        signal_type = np.array([
            1 if m.get("signal_type") == "long" else -1 if m.get("signal_type") == "short" else 0
            for m in metrics_sorted
        ], dtype=np.int8)

        is_signal = signal_type != 0
        is_long = signal_type == 1
        is_short = signal_type == -1

        outcome_values = np.array([
            m.get("outcome") if m.get("outcome") is not None else np.nan for m in metrics_sorted
        ], dtype=float)

        base_ts = np.array([
            _extract_timestamp_ms(m) or np.nan for m in metrics_sorted
        ], dtype=np.float64)

        outcome_ts = np.array([
            _extract_timestamp_ms({
                "timestamp": m.get("outcome_timestamp") or m.get("outcome_ts")
            })
            if m.get("outcome_timestamp") or m.get("outcome_ts") else np.nan
            for m in metrics_sorted
        ], dtype=np.float64)

        success_mask = np.isclose(outcome_values, 1.0, equal_nan=False)
        failure_mask = np.isclose(outcome_values, 0.0, equal_nan=False)
        evaluated_mask = success_mask | failure_mask

        valid_delay_mask = np.isfinite(outcome_ts) & np.isfinite(base_ts) & evaluated_mask & is_signal
        delay_seconds = np.zeros_like(base_ts, dtype=np.float64)
        if np.any(valid_delay_mask):
            delay_seconds[valid_delay_mask] = np.maximum(
                0.0,
                (outcome_ts[valid_delay_mask] - base_ts[valid_delay_mask]) / 1000.0,
            )

        cum_total = np.cumsum(is_signal.astype(np.float64))
        cum_long = np.cumsum(is_long.astype(np.float64))
        cum_short = np.cumsum(is_short.astype(np.float64))
        cum_success = np.cumsum((success_mask & is_signal).astype(np.float64))
        cum_failure = np.cumsum((failure_mask & is_signal).astype(np.float64))
        cum_delay_sum = np.cumsum(delay_seconds.astype(np.float64))
        cum_delay_count = np.cumsum(valid_delay_mask.astype(np.float64))

        metrics_tail: List[Dict[str, Any]]
        if len(metrics_sorted) > self.max_cached_metrics:
            metrics_tail = [dict(item) for item in metrics_sorted[-self.max_cached_metrics :]]
            metrics_offset = len(metrics_sorted) - len(metrics_tail)
        else:
            metrics_tail = [dict(item) for item in metrics_sorted]
            metrics_offset = 0

        payload = DomStatsPayload(
            symbol=safe_symbol,
            timestamps=timestamps,
            cum_total=cum_total,
            cum_long=cum_long,
            cum_short=cum_short,
            cum_success=cum_success,
            cum_failure=cum_failure,
            cum_delay_sum=cum_delay_sum,
            cum_delay_count=cum_delay_count,
            metrics_tail=metrics_tail,
            metrics_offset=metrics_offset,
            source_count=len(metrics_sorted),
            rebuilt_at=time.time(),
            max_timestamp=float(timestamps[-1]) if timestamps.size else None,
        )

        self._store_payload(safe_symbol, payload)

    # ------------------------------------------------------------------
    def _store_payload(self, safe_symbol: str, payload: DomStatsPayload) -> None:
        with self._lock:
            self._payloads[safe_symbol] = payload
        path = self._path_for_symbol(safe_symbol)
        tmp_path = f"{path}.tmp"
        try:
            with open(tmp_path, "wb") as fh:
                pickle.dump(
                    {
                        "version": self.VERSION,
                        "payload": payload,
                    },
                    fh,
                    protocol=pickle.HIGHEST_PROTOCOL,
                )
            os.replace(tmp_path, path)
        except Exception:
            logger.exception("Failed to persist DOM stats payload for %s", safe_symbol)
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    # ------------------------------------------------------------------
    def _load_payload(self, safe_symbol: str) -> Optional[DomStatsPayload]:
        path = self._path_for_symbol(safe_symbol)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "rb") as fh:
                record = pickle.load(fh)
            if not isinstance(record, dict):
                return None
            payload = record.get("payload")
            if isinstance(payload, DomStatsPayload):
                return payload
        except Exception:
            logger.exception("Failed to load DOM stats payload for %s", safe_symbol)
        return None

    # ------------------------------------------------------------------
    def _path_for_symbol(self, safe_symbol: str) -> str:
        return os.path.join(self.base_dir, f"dom_stats_{safe_symbol}.pkl")


__all__ = ["DomStatsAggregator"]
