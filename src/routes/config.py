"""API routes for the configuration center backed by Redis."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, Mapping

from fastapi import APIRouter, Depends, HTTPException, Request, status
from loguru import logger
from pydantic import BaseModel, Field

from src.cache import ensure_redis
from .auth import get_current_user


router = APIRouter()


HASH_PREFIX = "hash:config"
HISTORY_KEY_TEMPLATE = "list:config-history:{module}"
MODULE_SET_KEY = "set:config:modules"
UPDATE_CHANNEL = "pubsub:config:update"
AUDIT_LOG_PATH = Path("data/config_audit.log")
MAX_HISTORY_ENTRIES = 50


class ConfigUpdatePayload(BaseModel):
    """Request payload for updating a module configuration."""

    values: dict[str, Any] = Field(default_factory=dict)
    message: str | None = Field(default=None, max_length=500)


class RollbackPayload(BaseModel):
    """Request payload for rolling back a module to a previous revision."""

    version: str = Field(..., min_length=1)
    message: str | None = Field(default=None, max_length=500)


def _ensure_write_access(request: Request) -> None:
    if getattr(request.state, "is_read_only", False):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Read-only access")


def _stringify(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _normalise_values(values: Mapping[str, Any]) -> tuple[dict[str, str], list[str]]:
    to_set: dict[str, str] = {}
    to_remove: list[str] = []
    for raw_key, raw_value in values.items():
        key = str(raw_key).strip()
        if not key:
            continue
        if raw_value is None:
            to_remove.append(key)
        else:
            to_set[key] = _stringify(raw_value)
    return to_set, to_remove


async def _write_audit_log(entry: Mapping[str, Any]) -> None:
    AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False)

    def _append(path: Path, payload: str) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(payload + "\n")

    await asyncio.to_thread(partial(_append, AUDIT_LOG_PATH, line))


async def _load_history(redis: Any, module: str) -> list[dict[str, Any]]:
    history_key = HISTORY_KEY_TEMPLATE.format(module=module)
    try:
        raw_entries = await redis.lrange(history_key, 0, MAX_HISTORY_ENTRIES - 1)
    except Exception as exc:
        logger.warning("Failed to read config history", module=module, error=str(exc))
        return []

    entries: list[dict[str, Any]] = []
    for raw in raw_entries or []:
        try:
            entry = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


async def _append_history(redis: Any, module: str, entry: Mapping[str, Any]) -> None:
    history_key = HISTORY_KEY_TEMPLATE.format(module=module)
    payload = json.dumps(entry, ensure_ascii=False)
    try:
        await redis.lpush(history_key, payload)
        await redis.ltrim(history_key, 0, MAX_HISTORY_ENTRIES - 1)
    except Exception as exc:
        logger.warning("Failed to persist config history", module=module, error=str(exc))


def _serialise_mapping(values: Mapping[str, Any]) -> dict[str, str]:
    return {str(key): str(value) for key, value in values.items()}


async def _ensure_modules_entry(redis: Any, module: str) -> None:
    try:
        await redis.sadd(MODULE_SET_KEY, module)
    except Exception as exc:
        logger.warning("Failed to register config module", module=module, error=str(exc))


@router.get("/api/config/modules")
async def list_modules(user: str | None = Depends(get_current_user)) -> dict[str, list[str]]:
    redis = await ensure_redis()
    if redis is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Redis is unavailable")

    modules: set[str] = set()
    try:
        members = await redis.smembers(MODULE_SET_KEY)
        if members:
            modules.update(str(item) for item in members)
    except Exception as exc:
        logger.warning("Failed to read config module registry", error=str(exc))

    pattern = f"{HASH_PREFIX}:*"
    try:
        async for key in redis.scan_iter(match=pattern):  # type: ignore[attr-defined]
            text = str(key)
            module = text.split(":", 2)[-1]
            if module:
                modules.add(module)
    except AttributeError:
        logger.debug("Redis client does not support scan_iter; skipping hash discovery")
    except Exception as exc:
        logger.warning("Failed to scan config hashes", error=str(exc))

    return {"modules": sorted(modules)}


@router.get("/api/config/{module}")
async def read_module(module: str, user: str | None = Depends(get_current_user)) -> dict[str, Any]:
    redis = await ensure_redis()
    if redis is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Redis is unavailable")

    module = module.strip()
    if not module:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Module is required")

    key = f"{HASH_PREFIX}:{module}"
    try:
        values = await redis.hgetall(key)
    except Exception as exc:
        logger.error("Failed to load configuration", module=module, error=str(exc))
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to load configuration")

    return {"module": module, "values": _serialise_mapping(values or {})}


@router.get("/api/config/{module}/history")
async def read_history(module: str, user: str | None = Depends(get_current_user)) -> dict[str, Any]:
    redis = await ensure_redis()
    if redis is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Redis is unavailable")

    module = module.strip()
    if not module:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Module is required")

    entries = await _load_history(redis, module)
    return {"module": module, "entries": entries}


@router.put("/api/config/{module}")
async def update_module(
    module: str,
    payload: ConfigUpdatePayload,
    request: Request,
    user: str | None = Depends(get_current_user),
) -> dict[str, Any]:
    _ensure_write_access(request)

    redis = await ensure_redis()
    if redis is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Redis is unavailable")

    module = module.strip()
    if not module:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Module is required")

    key = f"{HASH_PREFIX}:{module}"
    try:
        before = await redis.hgetall(key)
    except Exception as exc:
        logger.error("Failed to load configuration before update", module=module, error=str(exc))
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to load configuration")

    to_set, to_remove = _normalise_values(payload.values)

    try:
        if to_set:
            await redis.hset(key, mapping=to_set)
        if to_remove:
            await redis.hdel(key, *to_remove)
        current = await redis.hgetall(key)
    except Exception as exc:
        logger.error("Failed to persist configuration", module=module, error=str(exc))
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to persist configuration")

    timestamp = datetime.now(timezone.utc).isoformat()
    version = str(uuid.uuid4())
    entry: dict[str, Any] = {
        "version": version,
        "module": module,
        "action": "update",
        "values": _serialise_mapping(current or {}),
        "updated_at": timestamp,
        "updated_by": user or "anonymous",
    }
    if payload.message:
        entry["message"] = payload.message.strip()
    if to_remove:
        entry["removed"] = sorted(set(to_remove))

    await _append_history(redis, module, entry)
    await _ensure_modules_entry(redis, module)

    try:
        await redis.publish(UPDATE_CHANNEL, module)
    except Exception as exc:
        logger.warning("Failed to publish config update", module=module, error=str(exc))

    audit_entry = dict(entry)
    audit_entry["before"] = _serialise_mapping(before or {})
    await _write_audit_log(audit_entry)

    return {
        "success": True,
        "module": module,
        "values": entry["values"],
        "version": version,
    }


@router.post("/api/config/{module}/rollback")
async def rollback_module(
    module: str,
    payload: RollbackPayload,
    request: Request,
    user: str | None = Depends(get_current_user),
) -> dict[str, Any]:
    _ensure_write_access(request)

    redis = await ensure_redis()
    if redis is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Redis is unavailable")

    module = module.strip()
    if not module:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Module is required")

    version = payload.version.strip()
    if not version:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Version is required")

    entries = await _load_history(redis, module)
    target = next((entry for entry in entries if entry.get("version") == version), None)
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Revision not found")

    desired_values = target.get("values", {}) if isinstance(target, dict) else {}
    if not isinstance(desired_values, Mapping):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Revision payload invalid")

    key = f"{HASH_PREFIX}:{module}"
    try:
        before = await redis.hgetall(key)
    except Exception as exc:
        logger.error("Failed to load configuration before rollback", module=module, error=str(exc))
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to load configuration")

    to_set = {str(field): _stringify(value) for field, value in desired_values.items()}
    current_keys = set(before.keys())
    target_keys = set(to_set.keys())
    to_remove = list(current_keys - target_keys)

    try:
        if to_set:
            await redis.hset(key, mapping=to_set)
        else:
            await redis.delete(key)
        if to_remove:
            await redis.hdel(key, *to_remove)
        current = await redis.hgetall(key)
    except Exception as exc:
        logger.error("Failed to apply rollback", module=module, error=str(exc))
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to persist configuration")

    timestamp = datetime.now(timezone.utc).isoformat()
    new_version = str(uuid.uuid4())
    entry: dict[str, Any] = {
        "version": new_version,
        "module": module,
        "action": "rollback",
        "restored_version": version,
        "values": _serialise_mapping(current or {}),
        "updated_at": timestamp,
        "updated_by": user or "anonymous",
    }
    if payload.message:
        entry["message"] = payload.message.strip()
    if to_remove:
        entry["removed"] = sorted(set(to_remove))

    await _append_history(redis, module, entry)
    await _ensure_modules_entry(redis, module)

    try:
        await redis.publish(UPDATE_CHANNEL, module)
    except Exception as exc:
        logger.warning("Failed to publish config rollback", module=module, error=str(exc))

    audit_entry = dict(entry)
    audit_entry["before"] = _serialise_mapping(before or {})
    await _write_audit_log(audit_entry)

    return {
        "success": True,
        "module": module,
        "values": entry["values"],
        "version": new_version,
    }

