import os
import pickle
import re
import time
from datetime import datetime
from threading import Lock
from typing import Any, Dict, List

from .utils import parse_local_datetime


DOM_RETENTION_ENV = "DOM_CACHE_RETENTION_DAYS"


def _resolve_retention_seconds(default_days: float = 1.0) -> int:
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
            container.setdefault("snapshots", []).append(snapshot_copy)
            container.setdefault("metrics", []).append(metrics_copy)
            self._prune_locked(key, container, now_ms=now_ms)
            last_save = self._last_save_ts.get(key, 0.0)
            if time.time() - last_save >= self.save_interval_seconds:
                self._save_locked(key, container)
                self._last_save_ts[key] = time.time()

    # ------------------------------------------------------------------
    def get_history(self, symbol: str) -> Dict[str, List[Dict[str, Any]]]:
        if not symbol:
            return {"snapshots": [], "metrics": []}
        key = self._sanitize_symbol(symbol)
        now_ms = time.time() * 1000.0
        with self._lock:
            container = self._load_locked(key)
            self._prune_locked(key, container, now_ms=now_ms)
            snapshots = [dict(item) for item in container.get("snapshots", [])]
            metrics = [dict(item) for item in container.get("metrics", [])]
        metrics.sort(key=lambda item: self._timestamp_ms(item))
        snapshots.sort(key=lambda item: self._timestamp_ms(item))

        # 保证 snapshots 长度不超过 10000（保留最新的 10000 条）
        snapshots = snapshots[-10000:]

        return {"snapshots": snapshots, "metrics": metrics}

    # ------------------------------------------------------------------
    def get_latest_metrics(self, symbol: str) -> Dict[str, Any]:
        history = self.get_history(symbol)
        metrics = history.get("metrics") or []
        if not metrics:
            return {}
        return dict(metrics[-1])


__all__ = ["DomCacheManager"]
