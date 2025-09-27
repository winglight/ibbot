"""Redis helpers for account and position snapshots.

This module centralises all Redis interactions related to account snapshots so
that different parts of the application can read/write cached data in a
consistent format.  Each snapshot stored in Redis is a JSON document with the
following shape::

    {
        "data": <payload>,
        "updated_at": "ISO timestamp",
        "source": "description of the writer",
        "stale": bool,
        "stale_reason": Optional[str],
        "stale_at": Optional[str]
    }

The ``stale`` flag allows callers to surface that the snapshot may no longer be
accurate (for example after a connection loss).  Writers should clear the flag
once fresh data is persisted.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, Optional

from loguru import logger

try:  # ``redis`` is optional during testing; callers should handle ``None``
    from redis.asyncio import Redis
except Exception:  # pragma: no cover - redis is optional in some tests
    Redis = None  # type: ignore

from .utils import now_local, to_local_iso


SNAPSHOT_ACCOUNT_INFO_KEY = "account:info"
SNAPSHOT_POSITIONS_KEY = "account:positions"

_redis_client: Optional[Redis] = None
_redis_lock = asyncio.Lock()
snapshot_logger = logger.bind(module="account_snapshot_cache", request_id="-")


def _get_redis_url() -> str:
    """Return the Redis connection URL from the environment."""

    return os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")


async def _ensure_client() -> Optional[Redis]:
    """Return a connected Redis client or ``None`` when unavailable."""

    global _redis_client

    if Redis is None:  # pragma: no cover - redis dependency optional
        snapshot_logger.debug("Redis client not available (module import failed)")
        return None

    async with _redis_lock:
        if _redis_client is None:
            try:
                _redis_client = Redis.from_url(
                    _get_redis_url(),
                    encoding="utf-8",
                    decode_responses=True,
                    health_check_interval=30,
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                snapshot_logger.warning(f"Failed to create Redis client: {exc}")
                _redis_client = None

    client = _redis_client
    if client is None:
        return None

    try:
        await client.ping()
    except Exception as exc:
        snapshot_logger.warning(f"Redis ping failed, resetting client: {exc}")
        try:
            await client.close()
        except Exception:
            pass
        _redis_client = None
        return None

    return client


async def _write_snapshot(key: str, data: Any, source: str, *, stale: bool = False) -> bool:
    """Persist a snapshot to Redis."""

    client = await _ensure_client()
    if client is None:
        return False

    payload = {
        "data": data,
        "updated_at": to_local_iso(now_local()),
        "source": source,
        "stale": stale,
    }
    if not stale:
        payload.pop("stale_reason", None)
        payload.pop("stale_at", None)

    try:
        await client.set(key, json.dumps(payload, ensure_ascii=False))
        return True
    except Exception as exc:
        snapshot_logger.warning(f"Failed to write snapshot '{key}': {exc}")
        return False


async def _read_snapshot(key: str) -> Optional[Dict[str, Any]]:
    """Load and deserialize a snapshot from Redis."""

    client = await _ensure_client()
    if client is None:
        return None

    try:
        raw = await client.get(key)
    except Exception as exc:
        snapshot_logger.warning(f"Failed to read snapshot '{key}': {exc}")
        return None

    if not raw:
        return None

    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception as exc:
        snapshot_logger.warning(f"Invalid snapshot payload for '{key}': {exc}")

    return None


async def _mark_snapshot_stale(key: str, reason: str) -> None:
    """Flag a snapshot as stale, preserving the original payload when present."""

    existing = await _read_snapshot(key)
    if existing is None:
        existing = {
            "data": None,
            "updated_at": to_local_iso(now_local()),
            "source": "stale",
        }

    existing["stale"] = True
    existing["stale_reason"] = reason
    existing["stale_at"] = to_local_iso(now_local())

    client = await _ensure_client()
    if client is None:
        return

    try:
        await client.set(key, json.dumps(existing, ensure_ascii=False))
    except Exception as exc:
        snapshot_logger.warning(f"Failed to flag snapshot '{key}' as stale: {exc}")


async def write_account_info_snapshot(data: Dict[str, Any], source: str) -> bool:
    """Persist account information to Redis."""

    return await _write_snapshot(SNAPSHOT_ACCOUNT_INFO_KEY, data, source, stale=False)


async def write_positions_snapshot(data: Any, source: str) -> bool:
    """Persist positions list to Redis."""

    return await _write_snapshot(SNAPSHOT_POSITIONS_KEY, data, source, stale=False)


async def read_account_info_snapshot() -> Optional[Dict[str, Any]]:
    """Return the latest account info snapshot when available."""

    return await _read_snapshot(SNAPSHOT_ACCOUNT_INFO_KEY)


async def read_positions_snapshot() -> Optional[Dict[str, Any]]:
    """Return the latest positions snapshot when available."""

    return await _read_snapshot(SNAPSHOT_POSITIONS_KEY)


async def mark_account_info_stale(reason: str) -> None:
    """Flag the cached account info as stale."""

    await _mark_snapshot_stale(SNAPSHOT_ACCOUNT_INFO_KEY, reason)


async def mark_positions_stale(reason: str) -> None:
    """Flag the cached positions snapshot as stale."""

    await _mark_snapshot_stale(SNAPSHOT_POSITIONS_KEY, reason)


async def mark_all_snapshots_stale(reason: str) -> None:
    """Mark both account info and positions snapshots as stale."""

    await asyncio.gather(
        mark_account_info_stale(reason),
        mark_positions_stale(reason),
        return_exceptions=True,
    )


async def clear_redis_client() -> None:
    """Close the current Redis client (primarily for tests)."""

    global _redis_client
    client = _redis_client
    _redis_client = None
    if client is not None:
        try:
            await client.close()
        except Exception:
            pass
