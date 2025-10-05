"""Helpers for persisting DOM configuration."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, Mapping, Optional

from loguru import logger

from src.ibbot.redis import get_redis_client

_DOM_CONFIG_KEY_ENV = "DOM_CONFIG_REDIS_KEY"
_DEFAULT_DOM_CONFIG_KEY = "json:dom:config"


async def _read_json_async(key: str) -> Optional[Dict[str, Any]]:
    try:
        client = await get_redis_client()
        raw = await client.get(key)
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.warning("Failed to load DOM payload from Redis", key=key, error=str(exc))
        return None

    if not raw:
        return None

    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        logger.warning("Invalid JSON payload in DOM store", key=key, error=str(exc))
        return None

    if not isinstance(data, dict):
        logger.warning("DOM store payload is not a mapping", key=key)
        return None

    return data


async def _write_json_async(key: str, payload: Mapping[str, Any]) -> None:
    serialisable: Dict[str, Any]
    try:
        serialisable = dict(payload)
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.warning("Failed to normalise DOM payload for Redis", key=key, error=str(exc))
        return

    try:
        client = await get_redis_client()
        await client.set(key, json.dumps(serialisable))
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.warning("Failed to persist DOM payload to Redis", key=key, error=str(exc))


def _resolve_key(redis_key: Optional[str], *, default: str, env: str) -> str:
    if redis_key:
        return redis_key
    return os.getenv(env, default)


def _load_payload(redis_key: str) -> Optional[Dict[str, Any]]:
    try:
        return asyncio.run(_read_json_async(redis_key))
    except RuntimeError:
        logger.debug(
            "DOM persistence load skipped because an event loop is already running",
            key=redis_key,
        )
        return None


def _persist_payload(redis_key: str, payload: Mapping[str, Any]) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_write_json_async(redis_key, payload))
        return

    loop.create_task(_write_json_async(redis_key, payload))


def load_dom_config(redis_key: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Return the persisted DOM configuration, if available."""

    key = _resolve_key(redis_key, default=_DEFAULT_DOM_CONFIG_KEY, env=_DOM_CONFIG_KEY_ENV)
    return _load_payload(key)


def save_dom_config(payload: Mapping[str, Any], redis_key: Optional[str] = None) -> None:
    """Persist the DOM configuration payload."""

    key = _resolve_key(redis_key, default=_DEFAULT_DOM_CONFIG_KEY, env=_DOM_CONFIG_KEY_ENV)
    _persist_payload(key, payload)


__all__ = [
    "load_dom_config",
    "save_dom_config",
]
