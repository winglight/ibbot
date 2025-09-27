"""Caching backends for the unified data layer."""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from typing import Any, Deque, Dict, Iterable, List, Optional

__all__ = [
    "CacheBackend",
    "InMemoryCacheBackend",
]


class CacheBackend:
    """Interface for snapshot/series caching used by adapters."""

    def set_snapshot(self, channel: str, symbol: str, payload: Dict[str, Any]) -> None:
        raise NotImplementedError

    def get_snapshot(self, channel: str, symbol: str) -> Dict[str, Any]:
        raise NotImplementedError

    def append_series(
        self,
        channel: str,
        symbol: str,
        payload: Dict[str, Any],
        *,
        limit: Optional[int] = None,
    ) -> None:
        raise NotImplementedError

    def get_series(
        self,
        channel: str,
        symbol: str,
        *,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        raise NotImplementedError


class InMemoryCacheBackend(CacheBackend):
    """Thread-safe in-memory cache suitable for tests and local runs."""

    def __init__(self) -> None:
        self._snapshots: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)
        self._series: Dict[str, Dict[str, Deque[Dict[str, Any]]]] = defaultdict(dict)
        self._lock = threading.Lock()

    def set_snapshot(self, channel: str, symbol: str, payload: Dict[str, Any]) -> None:
        with self._lock:
            channel_map = self._snapshots.setdefault(channel, {})
            channel_map[symbol] = dict(payload)

    def get_snapshot(self, channel: str, symbol: str) -> Dict[str, Any]:
        with self._lock:
            channel_map = self._snapshots.get(channel)
            if not channel_map:
                return {}
            payload = channel_map.get(symbol)
            return dict(payload) if payload else {}

    def append_series(
        self,
        channel: str,
        symbol: str,
        payload: Dict[str, Any],
        *,
        limit: Optional[int] = None,
    ) -> None:
        limit = int(limit) if limit else None
        with self._lock:
            channel_map = self._series.setdefault(channel, {})
            series = channel_map.get(symbol)
            if series is None:
                maxlen = limit if limit and limit > 0 else None
                series = deque(maxlen=maxlen)
                channel_map[symbol] = series
            if limit and series.maxlen != limit:
                series = deque(series, maxlen=limit)
                channel_map[symbol] = series
            series.append(dict(payload))

    def get_series(
        self,
        channel: str,
        symbol: str,
        *,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            channel_map = self._series.get(channel)
            if not channel_map:
                return []
            series = channel_map.get(symbol)
            if not series:
                return []
            if limit is None or limit <= 0:
                return [dict(item) for item in series]
            return [dict(item) for item in list(series)[-int(limit) :]]

