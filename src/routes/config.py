"""API routes for the configuration center backed by Redis."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, Mapping

from fastapi import APIRouter, Depends, HTTPException, Request, status
from loguru import logger
from pydantic import BaseModel, Field

from src.cache import ensure_redis
from src.ibbot.redis import RedisError
from .auth import get_current_user


router = APIRouter()


HASH_PREFIX = "hash:config"
HISTORY_KEY_TEMPLATE = "list:config-history:{module}"
MODULE_SET_KEY = "set:config:modules"
UPDATE_CHANNEL = "pubsub:config:update"
AUDIT_LOG_PATH = Path("data/config_audit.log")
MAX_HISTORY_ENTRIES = 50
DEFAULT_COMMAND_TIMEOUT = 2.0
DEFAULT_SCAN_TIMEOUT = 5.0
COMMAND_TIMEOUT_ENV = "CONFIG_CENTER_REDIS_COMMAND_TIMEOUT"
SCAN_TIMEOUT_ENV = "CONFIG_CENTER_REDIS_SCAN_TIMEOUT"
SCAN_ITER_COUNT = 500


def _load_command_timeout() -> float:
    raw = os.getenv(COMMAND_TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_COMMAND_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "Invalid %s value %r; falling back to %.2fs",
            COMMAND_TIMEOUT_ENV,
            raw,
            DEFAULT_COMMAND_TIMEOUT,
        )
        return DEFAULT_COMMAND_TIMEOUT
    if value <= 0:
        logger.warning(
            "%s must be positive; falling back to %.2fs",
            COMMAND_TIMEOUT_ENV,
            DEFAULT_COMMAND_TIMEOUT,
        )
        return DEFAULT_COMMAND_TIMEOUT
    return value


def _load_scan_timeout(base_timeout: float) -> float:
    raw = os.getenv(SCAN_TIMEOUT_ENV, "").strip()
    baseline = max(base_timeout, DEFAULT_SCAN_TIMEOUT)
    if not raw:
        return baseline
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "Invalid %s value %r; falling back to %.2fs",
            SCAN_TIMEOUT_ENV,
            raw,
            baseline,
        )
        return baseline
    if value <= 0:
        logger.warning(
            "%s must be positive; falling back to %.2fs",
            SCAN_TIMEOUT_ENV,
            baseline,
        )
        return baseline
    return max(base_timeout, value)


CONFIG_REDIS_COMMAND_TIMEOUT = _load_command_timeout()
CONFIG_REDIS_SCAN_TIMEOUT = _load_scan_timeout(CONFIG_REDIS_COMMAND_TIMEOUT)


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


async def _await_redis(
    awaitable: Any,
    *,
    action: str,
    module: str | None = None,
    suppress: bool = False,
    timeout: float | None = None,
) -> Any:
    effective_timeout = CONFIG_REDIS_COMMAND_TIMEOUT if timeout is None else float(timeout)
    detail_timeout = "Redis request timed out"
    detail_unavailable = "Redis is unavailable"
    log_module = module or "-"
    try:
        return await asyncio.wait_for(awaitable, timeout=effective_timeout)
    except asyncio.TimeoutError as exc:
        if suppress:
            logger.warning(
                "Redis command timed out",
                action=action,
                module=log_module,
                timeout=effective_timeout,
            )
            return None
        logger.error(
            "Redis command timed out",
            action=action,
            module=log_module,
            timeout=effective_timeout,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=detail_timeout,
        ) from exc
    except TimeoutError as exc:  # pragma: no cover - defensive for other libraries
        if suppress:
            logger.warning(
                "Redis command timed out (native)",
                action=action,
                module=log_module,
                timeout=effective_timeout,
            )
            return None
        logger.error(
            "Redis command timed out (native)",
            action=action,
            module=log_module,
            timeout=effective_timeout,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=detail_timeout,
        ) from exc
    except RedisError as exc:
        if suppress:
            logger.warning(
                "Redis command failed",
                action=action,
                module=log_module,
                error=str(exc),
            )
            return None
        logger.error(
            "Redis command failed",
            action=action,
            module=log_module,
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=detail_unavailable,
        ) from exc


async def _restore_previous_state(redis: Any, key: str, before: Mapping[str, Any], module: str) -> None:
    if before:
        await _await_redis(
            redis.hset(key, mapping=dict(before)),
            action="restore_hset",
            module=module,
            suppress=True,
        )
    else:
        await _await_redis(
            redis.delete(key),
            action="restore_delete",
            module=module,
            suppress=True,
        )


async def _scan_config_modules(redis: Any, pattern: str) -> list[str]:
    iterator = redis.scan_iter(match=pattern, count=SCAN_ITER_COUNT)  # type: ignore[attr-defined]
    modules: list[str] = []
    while True:
        try:
            key = await _await_redis(
                iterator.__anext__(),
                action="scan_iter",
                timeout=CONFIG_REDIS_SCAN_TIMEOUT,
            )
        except StopAsyncIteration:
            break
        if key is None:
            continue
        text = str(key)
        module = text.split(":", 2)[-1]
        if module:
            modules.append(module)
    return modules


# Backwards compatibility routes for legacy frontends that call the configuration
# API without the `/api` prefix. Keeping these aliases avoids client timeouts
# while the canonical paths remain under `/api`.
@router.get("/api/config/modules")
@router.get("/config/modules", include_in_schema=False)
async def list_modules(user: str | None = Depends(get_current_user)) -> dict[str, Any]:
    redis = await ensure_redis()
    if redis is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Redis is unavailable")

    modules: set[str] = set()
    errors: list[dict[str, Any]] = []

    try:
        members = await _await_redis(redis.smembers(MODULE_SET_KEY), action="smembers")
    except HTTPException as exc:
        if exc.status_code != status.HTTP_503_SERVICE_UNAVAILABLE:
            raise
        logger.warning("Config module registry lookup timed out")
        errors.append({"type": "redis_timeout", "context": "module_set", "message": "Redis 请求超时"})
        members = None
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Failed to read config module registry", error=str(exc))
        errors.append({"type": "redis_error", "context": "module_set", "message": str(exc)})
        members = None

    if members:
        modules.update(str(item) for item in members)

    pattern = f"{HASH_PREFIX}:*"
    try:
        discovered = await _scan_config_modules(redis, pattern)
        modules.update(discovered)
    except HTTPException as exc:
        if exc.status_code != status.HTTP_503_SERVICE_UNAVAILABLE:
            raise
        logger.warning("Config module discovery timed out")
        errors.append({"type": "redis_timeout", "context": "scan_iter", "message": "Redis 请求超时"})
    except AttributeError:
        logger.debug("Redis client does not support scan_iter; skipping hash discovery")
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Config module discovery failed", error=str(exc))
        errors.append({"type": "redis_error", "context": "scan_iter", "message": str(exc)})

    response: dict[str, Any] = {"modules": sorted(modules)}
    if errors:
        response["errors"] = errors
    return response


@router.get("/api/config/{module}")
@router.get("/config/{module}", include_in_schema=False)
async def read_module(module: str, user: str | None = Depends(get_current_user)) -> dict[str, Any]:
    redis = await ensure_redis()
    if redis is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Redis is unavailable")

    module = module.strip()
    if not module:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Module is required")

    key = f"{HASH_PREFIX}:{module}"
    try:
        values = await _await_redis(redis.hgetall(key), action="hgetall", module=module)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to load configuration", module=module, error=str(exc))
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to load configuration") from exc

    return {"module": module, "values": _serialise_mapping(values or {})}


@router.get("/api/config/{module}/history")
@router.get("/config/{module}/history", include_in_schema=False)
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
@router.put("/config/{module}", include_in_schema=False)
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
        before = await _await_redis(redis.hgetall(key), action="hgetall", module=module)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to load configuration before update", module=module, error=str(exc))
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to load configuration") from exc

    to_set, to_remove = _normalise_values(payload.values)

    changed = False
    try:
        if to_set:
            await _await_redis(redis.hset(key, mapping=to_set), action="hset", module=module)
            changed = True
        if to_remove:
            await _await_redis(redis.hdel(key, *to_remove), action="hdel", module=module)
            changed = True
        current = await _await_redis(redis.hgetall(key), action="hgetall", module=module)
    except HTTPException:
        if changed:
            await _restore_previous_state(redis, key, before or {}, module)
        raise
    except Exception as exc:
        logger.error("Failed to persist configuration", module=module, error=str(exc))
        if changed:
            await _restore_previous_state(redis, key, before or {}, module)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to persist configuration") from exc

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
@router.post("/config/{module}/rollback", include_in_schema=False)
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
        before = await _await_redis(redis.hgetall(key), action="hgetall", module=module)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to load configuration before rollback", module=module, error=str(exc))
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to load configuration") from exc

    to_set = {str(field): _stringify(value) for field, value in desired_values.items()}
    current_keys = set((before or {}).keys())
    target_keys = set(to_set.keys())
    to_remove = list(current_keys - target_keys)

    changed = False
    try:
        if to_set:
            await _await_redis(redis.hset(key, mapping=to_set), action="hset", module=module)
            changed = True
        else:
            await _await_redis(redis.delete(key), action="delete", module=module)
            changed = True
        if to_remove:
            await _await_redis(redis.hdel(key, *to_remove), action="hdel", module=module)
            changed = True
        current = await _await_redis(redis.hgetall(key), action="hgetall", module=module)
    except HTTPException:
        if changed:
            await _restore_previous_state(redis, key, before or {}, module)
        raise
    except Exception as exc:
        logger.error("Failed to apply rollback", module=module, error=str(exc))
        if changed:
            await _restore_previous_state(redis, key, before or {}, module)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to persist configuration") from exc

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
