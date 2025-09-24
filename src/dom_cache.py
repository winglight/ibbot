import os
import pickle
import re
import time
from threading import Lock
from typing import Any, Dict, List, Optional

from .utils import parse_local_datetime


DOM_RETENTION_ENV = "DOM_CACHE_RETENTION_DAYS"


def _resolve_retention_seconds(default_days: float = 7.0) -> int:
    """Resolve DOM cache retention in seconds using an environment variable."""

    env_value = os.getenv(DOM_RETENTION_ENV)
    days = default_days
    if env_value:
        try:
            days = float(env_value)
        except (TypeError, ValueError):
            days = default_days
    seconds = int(days * 24 * 60 * 60)
    # Ensure we always retain at least one minute of history
    return max(seconds, 60)


def _ensure_directory(path: str) -> None:
    os.makedirs(path, exist_ok=True)


class DomCacheManager:
    """Persist DOM snapshots and computed metrics to local pickle files."""

    def __init__(
        self,
        base_dir: str = "data",
        retention_seconds: int = None,
        save_interval_seconds: int = 30,
    ) -> None:
        self.base_dir = base_dir
        if retention_seconds is None:
            retention_seconds = _resolve_retention_seconds()
        self.retention_seconds = max(int(retention_seconds), 60)
        self.save_interval_seconds = max(save_interval_seconds, 1)
        self._lock = Lock()
        self._containers: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        self._last_save_ts: Dict[str, float] = {}
        _ensure_directory(self.base_dir)

    # ------------------------------------------------------------------
    def _sanitize_symbol(self, symbol: str) -> str:
        if not symbol:
            return "UNKNOWN"
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", symbol.upper())
        return safe or "UNKNOWN"

    # ------------------------------------------------------------------
    def _file_path(self, symbol: str) -> str:
        safe = self._sanitize_symbol(symbol)
        return os.path.join(self.base_dir, f"dom_cache_{safe}.pkl")

    # ------------------------------------------------------------------
    def _timestamp_ms(self, item: Dict[str, Any]) -> float:
        ts = item.get("timestamp")
        if ts is None:
            ts = item.get("timestamp_ms")
        if ts is None and "timestamp_ns" in item:
            ts = item["timestamp_ns"] / 1_000_000.0
        if isinstance(ts, str):
            try:
                dt = parse_local_datetime(ts)
                ts = dt.timestamp() * 1000.0 if dt else None
            except ValueError:
                ts = None
        if ts is None:
            return 0.0
        try:
            return float(ts)
        except (TypeError, ValueError):
            return 0.0

    # ------------------------------------------------------------------
    def _prune_locked(
        self,
        key: str,
        container: Dict[str, List[Dict[str, Any]]],
        now_ms: float,
    ) -> None:
        cutoff = now_ms - self.retention_seconds * 1000.0
        for bucket in ("snapshots", "metrics"):
            items = container.get(bucket) or []
            if not items:
                continue
            container[bucket] = [
                item for item in items if self._timestamp_ms(item) >= cutoff
            ]

    # ------------------------------------------------------------------
    def _load_locked(self, key: str) -> Dict[str, List[Dict[str, Any]]]:
        if key in self._containers:
            return self._containers[key]
        path = self._file_path(key)
        snapshots: List[Dict[str, Any]] = []
        metrics: List[Dict[str, Any]] = []
        if os.path.exists(path):
            try:
                with open(path, "rb") as fh:
                    payload = pickle.load(fh)
                snapshots = list(payload.get("snapshots", []))
                metrics = list(payload.get("metrics", []))
            except Exception:
                snapshots, metrics = [], []
        container = {"snapshots": snapshots, "metrics": metrics}
        now_ms = time.time() * 1000.0
        self._prune_locked(key, container, now_ms=now_ms)
        self._containers[key] = container
        return container

    # ------------------------------------------------------------------
    def _save_locked(self, key: str, container: Dict[str, List[Dict[str, Any]]]) -> None:
        path = self._file_path(key)
        tmp_path = f"{path}.tmp"
        payload = {
            "snapshots": container.get("snapshots", []),
            "metrics": container.get("metrics", []),
        }
        try:
            with open(tmp_path, "wb") as fh:
                pickle.dump(payload, fh)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    # ------------------------------------------------------------------
    def append(
        self,
        symbol: str,
        snapshot: Dict[str, Any],
        metrics: Dict[str, Any],
    ) -> None:
        if not symbol:
            return
        key = self._sanitize_symbol(symbol)
        now_ms = self._timestamp_ms(metrics) or self._timestamp_ms(snapshot)
        if not now_ms:
            now_ms = time.time() * 1000.0
        snapshot_copy = dict(snapshot)
        metrics_copy = dict(metrics)
        snapshot_copy.setdefault("timestamp", now_ms)
        metrics_copy.setdefault("timestamp", now_ms)

        with self._lock:
            container = self._load_locked(key)
            metrics_list = container.setdefault("metrics", [])
            current_ts = self._timestamp_ms(metrics_copy)
            if metrics_list:
                last_ts = self._timestamp_ms(metrics_list[-1])
                if last_ts and current_ts and current_ts <= last_ts:
                    current_ts = last_ts + 0.001
                    metrics_copy["timestamp"] = current_ts
                    snapshot_copy["timestamp"] = current_ts
            metrics_list.append(metrics_copy)
            snapshots_list = container.setdefault("snapshots", [])
            snapshots_list.append(snapshot_copy)
            now_ms = current_ts or now_ms
            self._prune_locked(key, container, now_ms=now_ms)
            last_save = self._last_save_ts.get(key, 0.0)
            if time.time() - last_save >= self.save_interval_seconds:
                self._save_locked(key, container)
                self._last_save_ts[key] = time.time()

    # ------------------------------------------------------------------
    def update_metric(
        self,
        symbol: str,
        *,
        signal_id: Optional[str] = None,
        timestamp: Optional[float] = None,
        updates: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Update a cached metric entry identified by signal id or timestamp."""

        if not symbol or not updates:
            return False

        key = self._sanitize_symbol(symbol)
        with self._lock:
            container = self._load_locked(key)
            metrics_list = container.get("metrics") or []
            target: Optional[Dict[str, Any]] = None

            if signal_id is not None:
                for item in reversed(metrics_list):
                    if str(item.get("signal_id")) == str(signal_id):
                        target = item
                        break

            if target is None and timestamp is not None:
                try:
                    ts_target = float(timestamp)
                except (TypeError, ValueError):
                    ts_target = None
                if ts_target is not None:
                    for item in reversed(metrics_list):
                        ts_value = self._timestamp_ms(item)
                        if not ts_value:
                            continue
                        if abs(ts_value - ts_target) <= 0.5:
                            target = item
                            break

            if target is None:
                return False

            target.update(dict(updates))
            self._save_locked(key, container)
            self._last_save_ts[key] = time.time()
            return True

    # ------------------------------------------------------------------
    def _coerce_timestamp_ms(self, value: Any) -> Optional[float]:
        """Coerce timestamps provided as strings or numbers into milliseconds."""

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

    def get_history(
        self,
        symbol: str,
        *,
        limit: Optional[int] = 500,
        start_time: Optional[Any] = None,
        end_time: Optional[Any] = None,
        include_snapshots: bool = True,
        copy: bool = True,
    ) -> Dict[str, Any]:
        if not symbol:
            return {
                "snapshots": [] if include_snapshots else [],
                "metrics": [],
                "total_snapshots": 0,
                "total_metrics": 0,
            }

        key = self._sanitize_symbol(symbol)
        now_ms = time.time() * 1000.0
        with self._lock:
            container = self._load_locked(key)
            self._prune_locked(key, container, now_ms=now_ms)
            snapshots_source = container.get("snapshots", [])
            metrics_source = container.get("metrics", [])
            if copy and include_snapshots:
                snapshots = [dict(item) for item in snapshots_source]
            else:
                snapshots = list(snapshots_source)
            if copy:
                metrics = [dict(item) for item in metrics_source]
            else:
                metrics = list(metrics_source)

        start_ms = self._coerce_timestamp_ms(start_time)
        end_ms = self._coerce_timestamp_ms(end_time)

        limit_value: Optional[int] = None
        if limit is not None:
            try:
                limit_candidate = int(limit)
            except (TypeError, ValueError):
                limit_candidate = 0
            if limit_candidate > 0:
                limit_value = limit_candidate

        def _filter_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            filtered: List[Dict[str, Any]] = []
            for item in items:
                ts = self._timestamp_ms(item)
                if start_ms is not None and ts < start_ms:
                    continue
                if end_ms is not None and ts > end_ms:
                    continue
                filtered.append(item)
            filtered.sort(key=lambda payload: self._timestamp_ms(payload))
            return filtered

        metrics = _filter_items(metrics)
        total_metrics = len(metrics)
        if limit_value is not None:
            metrics = metrics[-limit_value:]

        process_snapshots = (
            include_snapshots
            or start_ms is not None
            or end_ms is not None
            or limit_value is not None
        )

        if process_snapshots:
            snapshots = _filter_items(snapshots)
            total_snapshots = len(snapshots)
            if limit_value is not None:
                snapshots = snapshots[-limit_value:]
            result_snapshots = snapshots if include_snapshots else []
        else:
            total_snapshots = len(snapshots_source)
            result_snapshots = [] if not include_snapshots else list(snapshots_source)

        result: Dict[str, Any] = {
            "snapshots": result_snapshots,
            "metrics": metrics,
            "total_snapshots": total_snapshots,
            "total_metrics": total_metrics,
        }
        return result

    # ------------------------------------------------------------------
    def get_latest_metrics(self, symbol: str) -> Dict[str, Any]:
        history = self.get_history(symbol)
        metrics = history.get("metrics") or []
        if not metrics:
            return {}
        return dict(metrics[-1])


__all__ = ["DomCacheManager"]
