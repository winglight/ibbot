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
from datetime import datetime
from typing import Any, Dict, Iterable, Optional, Tuple

from loguru import logger

try:  # ``redis`` is optional during testing; callers should handle ``None``
    from redis.asyncio import Redis
except Exception:  # pragma: no cover - redis is optional in some tests
    Redis = None  # type: ignore

from ibbot.config import load_account_snapshot_config

from .utils import now_local, to_local_iso, parse_local_datetime
from .utils.account_pnl_cache import write_net_liquidation_snapshot


SNAPSHOT_ACCOUNT_INFO_KEY = "account:info"
SNAPSHOT_POSITIONS_KEY = "account:positions"

_redis_client: Optional[Redis] = None
_redis_lock = asyncio.Lock()
_snapshot_config = load_account_snapshot_config()
SNAPSHOT_STREAM_KEY = _snapshot_config.snapshot_stream_key
SNAPSHOT_STREAM_LOOKBACK = 50
SNAPSHOT_HEALTH_HASH_KEY = _snapshot_config.health_hash_key

snapshot_logger = logger.bind(module="account_snapshot_cache", request_id="-")


def _get_redis_url() -> str:
    """Return the Redis connection URL from the environment."""
    # Dedicated override for the snapshot cache so environments can point the
    # cache at a different Redis instance when needed.
    explicit_url = os.getenv("ACCOUNT_CACHE_REDIS_URL")
    if explicit_url:
        return explicit_url

    # Reuse the shared REDIS_URL if defined; otherwise fall back to the data
    # layer URL (which may already embed a password) and finally construct a
    # URL from individual host/port/db/password variables.
    shared_url = os.getenv("REDIS_URL") or os.getenv("DATA_LAYER_REDIS_URL")
    if shared_url:
        return shared_url

    host = os.getenv("REDIS_HOST", "127.0.0.1")
    port = os.getenv("REDIS_PORT", "6379")
    db = os.getenv("REDIS_DB", "0")
    password = os.getenv("REDIS_PASSWORD")

    if password:
        return f"redis://:{password}@{host}:{port}/{db}"

    return f"redis://{host}:{port}/{db}"


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


def _bool_to_flag(value: bool) -> str:
    return "1" if value else "0"


def _normalise_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return None


def _maybe_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except Exception:  # pragma: no cover - defensive guard
            value = str(value)
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def _serialise_snapshot_payload(payload: Dict[str, Any]) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for key, value in payload.items():
        if value is None:
            continue
        if key == "data":
            try:
                mapping[key] = json.dumps(value, ensure_ascii=False)
            except Exception:
                mapping[key] = json.dumps({"repr": repr(value)}, ensure_ascii=False)
        elif isinstance(value, bool):
            mapping[key] = _bool_to_flag(value)
        elif isinstance(value, (dict, list)):
            mapping[key] = json.dumps(value, ensure_ascii=False)
        else:
            mapping[key] = str(value)
    return mapping


def _decode_snapshot_mapping(mapping: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not mapping:
        return None

    snapshot: Dict[str, Any] = {}

    data_value = mapping.get("data")
    if data_value is not None:
        snapshot["data"] = _maybe_json(data_value)
    else:
        meta_fields = {
            "updated_at",
            "source",
            "stale",
            "stale_reason",
            "stale_at",
            "cache_source",
            "cache_age_seconds",
        }
        additional: Dict[str, Any] = {}
        for key, value in mapping.items():
            if key in meta_fields:
                continue
            additional[key] = _maybe_json(value)
        if additional:
            snapshot["data"] = additional

    for key, value in mapping.items():
        if key == "data":
            continue
        if key == "stale":
            decoded = _normalise_bool(value)
            snapshot[key] = decoded if decoded is not None else value
        elif key.endswith("_seconds"):
            try:
                snapshot[key] = float(value)
            except (TypeError, ValueError):
                snapshot[key] = value
        else:
            snapshot[key] = value

    if "stale" not in snapshot:
        snapshot["stale"] = False

    return snapshot


async def _write_snapshot(
    key: str,
    data: Any,
    source: str,
    *,
    stale: bool = False,
    updated_at: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> bool:
    """Persist a snapshot to Redis."""

    client = await _ensure_client()
    if client is None:
        return False

    timestamp = updated_at or to_local_iso(now_local())

    payload: Dict[str, Any] = {
        "data": data,
        "updated_at": timestamp,
        "source": source,
        "stale": stale,
    }

    if meta:
        for field, value in meta.items():
            if field in {"data", "source", "stale", "updated_at"}:
                continue
            payload[field] = value

    mapping = _serialise_snapshot_payload(payload)

    pipe = client.pipeline(transaction=True)
    pipe.delete(key)
    pipe.hset(key, mapping=mapping)

    try:
        await pipe.execute()
        return True
    except Exception as exc:
        snapshot_logger.warning(f"Failed to write snapshot '{key}': {exc}")
        return False


async def _read_snapshot(key: str) -> Optional[Dict[str, Any]]:
    """Load and deserialize a snapshot from Redis."""

    client = await _ensure_client()
    if client is None:
        return None

    mapping: Dict[str, Any] = {}

    try:
        mapping = await client.hgetall(key)
    except Exception as exc:
        snapshot_logger.warning(f"Failed to read snapshot hash '{key}': {exc}")
        mapping = {}

    decoded = _decode_snapshot_mapping(mapping)
    if decoded is not None:
        return decoded

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


async def _read_snapshot_from_stream(command: str, *, lookback: int = SNAPSHOT_STREAM_LOOKBACK) -> Optional[Dict[str, Any]]:
    client = await _ensure_client()
    if client is None:
        return None

    try:
        entries: Iterable[Tuple[str, Dict[str, Any]]] = await client.xrevrange(
            SNAPSHOT_STREAM_KEY,
            count=max(1, lookback),
        )
    except Exception as exc:
        snapshot_logger.debug(
            "Failed to read account snapshot stream",
            error=str(exc),
        )
        return None

    for entry in entries:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            continue
        entry_id, fields = entry
        if not isinstance(fields, dict):
            continue
        if fields.get("command") != command:
            continue
        status = str(fields.get("status") or "").lower()
        if status and status not in {"ok", "success"}:
            continue
        payload = fields.get("payload")
        decoded_payload = _maybe_json(payload)
        snapshot: Dict[str, Any] = {
            "data": decoded_payload,
            "updated_at": fields.get("timestamp"),
            "source": fields.get("source") or "account_snapshot_stream",
            "stale": False,
            "command_id": fields.get("command_id"),
            "stream_id": entry_id,
            "status": fields.get("status"),
            "reason": fields.get("reason"),
        }
        return snapshot

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

    meta_payload = {
        field: value
        for field, value in existing.items()
        if field not in {"data", "source", "stale", "updated_at"}
    }
    meta_payload["stale_reason"] = reason
    meta_payload["stale_at"] = to_local_iso(now_local())
    meta_payload.pop("stale", None)

    await _write_snapshot(
        key,
        existing.get("data"),
        existing.get("source") or "stale",
        stale=True,
        updated_at=existing.get("updated_at"),
        meta=meta_payload,
    )


async def write_account_info_snapshot(data: Dict[str, Any], source: str) -> bool:
    """Persist account information to Redis."""

    success = await _write_snapshot(SNAPSHOT_ACCOUNT_INFO_KEY, data, source, stale=False)

    if success:
        net_value: Optional[float] = None
        for key in ("net_liquidation", "netLiquidation", "net_liquidation_value"):
            if key not in data:
                continue
            try:
                candidate = float(data[key])
            except (TypeError, ValueError):
                continue
            net_value = candidate
            break

        if net_value is not None:
            timestamp = None
            for ts_key in ("timestamp", "updated_at", "received_at", "refreshed_at"):
                raw_ts = data.get(ts_key)
                if isinstance(raw_ts, datetime):
                    timestamp = raw_ts
                    break
                if isinstance(raw_ts, str):
                    parsed = parse_local_datetime(raw_ts)
                    if parsed is not None:
                        timestamp = parsed
                        break
            try:
                await write_net_liquidation_snapshot(
                    net_value,
                    source=source,
                    timestamp=timestamp,
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                snapshot_logger.debug(
                    "Failed to update net liquidation snapshot", error=str(exc)
                )

    return success


async def write_positions_snapshot(data: Any, source: str) -> bool:
    """Persist positions list to Redis."""

    return await _write_snapshot(SNAPSHOT_POSITIONS_KEY, data, source, stale=False)


async def read_account_info_snapshot() -> Optional[Dict[str, Any]]:
    """Return the latest account info snapshot when available."""

    snapshot = await _read_snapshot(SNAPSHOT_ACCOUNT_INFO_KEY)
    if snapshot is not None:
        return snapshot
    return await _read_snapshot_from_stream("get_account_info")


async def read_positions_snapshot() -> Optional[Dict[str, Any]]:
    """Return the latest positions snapshot when available."""

    snapshot = await _read_snapshot(SNAPSHOT_POSITIONS_KEY)
    if snapshot is not None:
        return snapshot
    return await _read_snapshot_from_stream("get_positions")


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


async def read_account_service_health() -> Dict[str, Any]:
    """Read the health information published by the account snapshot service."""

    client = await _ensure_client()
    if client is None:
        return {}

    try:
        mapping = await client.hgetall(SNAPSHOT_HEALTH_HASH_KEY)
    except Exception as exc:
        snapshot_logger.debug(
            "Failed to read account snapshot health hash",
            error=str(exc),
        )
        return {}

    if not mapping:
        return {}

    health: Dict[str, Any] = {}
    for key, value in mapping.items():
        if key in {"queue_depth", "pending", "inflight"}:
            try:
                health[key] = int(value)
            except (TypeError, ValueError):
                health[key] = value
        elif key.endswith("_seconds"):
            try:
                health[key] = float(value)
            except (TypeError, ValueError):
                health[key] = value
        else:
            health[key] = value

    return health


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
