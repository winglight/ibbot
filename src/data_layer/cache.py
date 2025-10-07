"""Caching backends for the unified data layer."""

from __future__ import annotations

import json
import threading
from collections import defaultdict, deque
from typing import Any, Deque, Dict, List, Optional

from loguru import logger

try:  # pragma: no cover - optional dependency
    import redis  # type: ignore
except ImportError:  # pragma: no cover - redis is optional in tests
    redis = None

__all__ = [
    "CacheBackend",
    "InMemoryCacheBackend",
    "RedisCacheBackend",
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


class RedisCacheBackend(CacheBackend):
    """Redis-backed cache for sharing snapshots and recent series."""

    def __init__(
        self,
        url: str,
        *,
        prefix: str = "market-data",
        snapshot_ttl: Optional[int] = None,
    ) -> None:
        if redis is None:
            raise RuntimeError("redis package is not installed")

        self._client = redis.Redis.from_url(url)
        self._prefix = prefix.rstrip(":")
        self._snapshot_ttl = snapshot_ttl

    # Key helpers -----------------------------------------------------
    def _snapshot_key(self, channel: str, symbol: str) -> str:
        return f"{self._prefix}:snapshot:{channel}:{symbol}"

    def _series_key(self, channel: str, symbol: str) -> str:
        return f"{self._prefix}:series:{channel}:{symbol}"

    # CacheBackend API ------------------------------------------------
    def set_snapshot(self, channel: str, symbol: str, payload: Dict[str, Any]) -> None:
        key = self._snapshot_key(channel, symbol)
        data = json.dumps(payload, default=str)
        try:
            pipe = self._client.pipeline()
            pipe.set(key, data)
            if self._snapshot_ttl and self._snapshot_ttl > 0:
                pipe.expire(key, self._snapshot_ttl)
            pipe.execute()
        except Exception as exc:  # pragma: no cover - network errors
            logger.warning("Redis set_snapshot failed: %s", exc)

    def get_snapshot(self, channel: str, symbol: str) -> Dict[str, Any]:
        key = self._snapshot_key(channel, symbol)
        try:
            value = self._client.get(key)
            if not value:
                return {}
            if isinstance(value, (bytes, bytearray)):
                value = value.decode("utf-8")
            return json.loads(value)
        except Exception as exc:  # pragma: no cover - network errors
            logger.warning("Redis get_snapshot failed: %s", exc)
            return {}

    def append_series(
        self,
        channel: str,
        symbol: str,
        payload: Dict[str, Any],
        *,
        limit: Optional[int] = None,
    ) -> None:
        key = self._series_key(channel, symbol)
        data = json.dumps(payload, default=str)
        try:
            pipe = self._client.pipeline()
            pipe.rpush(key, data)
            if limit and limit > 0:
                pipe.ltrim(key, -limit, -1)
            pipe.execute()
        except Exception as exc:  # pragma: no cover - network errors
            logger.warning("Redis append_series failed: %s", exc)

    def get_series(
        self,
        channel: str,
        symbol: str,
        *,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        key = self._series_key(channel, symbol)
        try:
            if limit and limit > 0:
                values = self._client.lrange(key, -limit, -1)
            else:
                values = self._client.lrange(key, 0, -1)
        except Exception as exc:  # pragma: no cover - network errors
            logger.warning("Redis get_series failed: %s", exc)
            return []

        series: List[Dict[str, Any]] = []
        for item in values:
            if isinstance(item, (bytes, bytearray)):
                item = item.decode("utf-8")
            try:
                series.append(json.loads(item))
            except Exception:
                continue
        return series
