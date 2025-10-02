"""Helpers for aggregating service health information."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, MutableMapping, Sequence

from loguru import logger

from src.account_service_readiness import refresh_account_service_status
from src.ib_command_health import (
    IB_COMMAND_HEALTH_KEY,
    IB_COMMAND_HEALTH_STALE_AFTER_SEC,
    build_ib_connection_health_payload,
)
from src.ibbot.redis import get_redis_client
from src.routes.service_registry import (
    DOM_SERVICE_NAME,
    SERVICE_REGISTRY,
    ServiceRegistryEntry,
    registry_as_mappings,
)


__all__ = [
    "ServiceStatus",
    "gather_service_statuses",
    "build_service_status_payload",
]


@dataclass(slots=True)
class ServiceStatus:
    """Normalised representation of service state for UI consumption."""

    service: str
    status: str
    detail: str | None = None
    last_reported: str | None = None
    payload: Mapping[str, Any] | None = None

    def as_mapping(self) -> MutableMapping[str, Any]:
        data: MutableMapping[str, Any] = {
            "service": self.service,
            "status": self.status,
        }
        if self.detail:
            data["detail"] = self.detail
        if self.last_reported:
            data["last_reported"] = self.last_reported
        if self.payload is not None:
            data["payload"] = dict(self.payload)
        return data


_STATUS_NORMALISATION = {
    "running": "running",
    "online": "running",
    "ready": "running",
    "ok": "running",
    "healthy": "running",
    "started": "running",
    "up": "running",
    "idle": "running",
    "success": "running",
    "stopped": "offline",
    "offline": "offline",
    "down": "offline",
    "disabled": "offline",
    "paused": "offline",
    "error": "unhealthy",
    "failed": "unhealthy",
    "failing": "unhealthy",
    "unhealthy": "unhealthy",
    "warning": "unhealthy",
    "degraded": "unhealthy",
}

_REGISTRY_BY_ID: dict[str, ServiceRegistryEntry] = {
    entry.service_id: entry for entry in SERVICE_REGISTRY
}

_QUEUE_REGISTRY_ENTRY = _REGISTRY_BY_ID.get("queue_monitor")
QUEUE_MONITOR_HEALTH_HASH_KEY = (
    _QUEUE_REGISTRY_ENTRY.health.resolved_key()
    if _QUEUE_REGISTRY_ENTRY and _QUEUE_REGISTRY_ENTRY.health
    else os.getenv("QUEUE_MONITOR_HEALTH_KEY", "hash:service:health:queue")
)
QUEUE_MONITOR_METRICS_HASH_KEY = os.getenv(
    "QUEUE_MONITOR_METRICS_HASH_KEY", "hash:service:queue-monitor"
)


async def _close_redis(redis: Any) -> None:
    close = getattr(redis, "close", None)
    if close is None:
        return
    result = close()
    if asyncio.iscoroutine(result):
        try:
            await result
        except Exception:  # pragma: no cover - defensive cleanup
            logger.debug(
                "Ignoring redis.close() error during service status cleanup",
                exc_info=True,
            )


def _coerce_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "healthy", "running", "online"}:
        return True
    if text in {"0", "false", "no", "off", "unhealthy", "stopped", "offline"}:
        return False
    return None


def _normalize_status_value(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip().lower()
    return _STATUS_NORMALISATION.get(text)


def _normalise_timestamp(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


async def _load_hash(redis: Any, key: str) -> MutableMapping[str, Any] | None:
    try:
        values = await redis.hgetall(key)
    except Exception as exc:  # pragma: no cover - depends on runtime redis availability
        logger.debug("Failed to load health hash %s: %s", key, exc)
        return None
    if not values:
        return None
    return dict(values)


def _resolve_health_key(entry: ServiceRegistryEntry) -> str | None:
    health_def = entry.health
    if health_def is None:
        return None
    key = health_def.resolved_key()
    if not key:
        return None
    return key


def _missing_detail(entry: ServiceRegistryEntry, fallback: str = "未上报健康数据") -> str:
    health_def = entry.health
    if health_def and health_def.missing_detail:
        return health_def.missing_detail
    return fallback


def _build_status_from_payload(
    service: str, payload: Mapping[str, Any]
) -> ServiceStatus:
    status_value = _normalize_status_value(
        payload.get("status") or payload.get("state")
    )
    healthy_flag = _coerce_bool(payload.get("healthy"))
    stale_flag = _coerce_bool(payload.get("stale"))
    last_error = payload.get("last_error") or payload.get("error")
    last_reported = _normalise_timestamp(
        payload.get("last_seen")
        or payload.get("checked_at")
        or payload.get("timestamp")
    )

    resolved_status = status_value
    detail_parts: list[str] = []

    if resolved_status is None and healthy_flag is not None:
        resolved_status = "running" if healthy_flag else "unhealthy"
    if resolved_status is None and stale_flag:
        resolved_status = "unhealthy"
        detail_parts.append("健康数据已过期")
    if resolved_status is None and last_error:
        resolved_status = "unhealthy"
    if resolved_status is None:
        resolved_status = "running"
        detail_parts.append("服务未提供明确状态，默认显示运行中")

    if resolved_status == "running" and last_error:
        detail_parts.append(f"最近错误：{last_error}")
    elif resolved_status == "unhealthy" and last_error:
        detail_parts.append(f"错误信息：{last_error}")

    detail = "；".join(part for part in detail_parts if part)

    return ServiceStatus(
        service=service,
        status=resolved_status,
        detail=detail or None,
        last_reported=last_reported,
        payload=payload,
    )


async def _build_ib_command_status(redis: Any) -> ServiceStatus:
    raw = await _load_hash(redis, IB_COMMAND_HEALTH_KEY) or {}
    payload = build_ib_connection_health_payload(
        raw,
        stale_after=float(IB_COMMAND_HEALTH_STALE_AFTER_SEC or 30),
    )
    connected = bool(payload.get("connected"))
    stale = bool(payload.get("stale"))
    queue_depth = payload.get("queue_depth")
    last_error = payload.get("last_error")
    last_seen = payload.get("last_seen")

    if connected:
        status = "running"
        detail = f"队列深度 {queue_depth}" if queue_depth is not None else "队列健康"
    elif stale:
        status = "unhealthy"
        detail = "健康数据已过期"
    else:
        status = "unhealthy"
        detail = last_error or "连接状态异常"

    if last_error and status == "running":
        detail = f"最近错误：{last_error}"

    return ServiceStatus(
        service="ib_command",
        status=status,
        detail=detail,
        last_reported=last_seen,
        payload=payload,
    )


async def _build_queue_monitor_status(
    redis: Any | None, entry: ServiceRegistryEntry
) -> ServiceStatus:
    if redis is None:
        detail = (
            entry.external_detail
            if not entry.internal and entry.external_detail
            else "未提供 Redis 连接，无法加载队列指标。"
        )
        return ServiceStatus(
            service=entry.service_id,
            status="unhealthy",
            detail=detail,
        )

    health_payload = await _load_hash(redis, QUEUE_MONITOR_HEALTH_HASH_KEY)
    snapshot_payload = await _load_hash(redis, QUEUE_MONITOR_METRICS_HASH_KEY)

    if not health_payload and not snapshot_payload:
        detail = (
            entry.health.missing_detail
            if entry.health and entry.health.missing_detail
            else "未上报队列指标，检查 queue-monitor 服务。"
        )
        return ServiceStatus(
            service=entry.service_id,
            status="unhealthy",
            detail=detail,
            last_reported=None,
            payload=None,
        )

    combined_payload: dict[str, Any] = {}
    if health_payload:
        combined_payload.update(dict(health_payload))
    if snapshot_payload:
        combined_payload.setdefault("metrics_snapshot", dict(snapshot_payload))

    last_reported = _normalise_timestamp(
        (health_payload or {}).get("last_seen")
        or (snapshot_payload or {}).get("timestamp")
    )
    detail = "队列指标已更新"
    snapshot_timestamp = _normalise_timestamp((snapshot_payload or {}).get("timestamp"))
    if snapshot_timestamp:
        try:
            ts_dt = datetime.fromisoformat(snapshot_timestamp)
        except ValueError:
            ts_dt = None
        else:
            now = datetime.now(timezone.utc)
            if (now - ts_dt).total_seconds() > 60:
                detail = "队列指标超过 60 秒未更新"
                return ServiceStatus(
                    service="queue_monitor",
                    status="unhealthy",
                    detail=detail,
                    last_reported=snapshot_timestamp,
                    payload=combined_payload or None,
                )

    return ServiceStatus(
        service=entry.service_id,
        status="running",
        detail=detail,
        last_reported=last_reported,
        payload=combined_payload or None,
    )


def _build_dom_placeholder_status(entry: ServiceRegistryEntry) -> ServiceStatus:
    detail = entry.external_detail or "DOM 服务由外部部署提供（external/not provisioned）。"
    return ServiceStatus(
        service=entry.service_id,
        status="offline",
        detail=detail,
    )


async def _build_dom_service_status(
    redis: Any | None, entry: ServiceRegistryEntry
) -> ServiceStatus:
    health_def = entry.health
    dom_key = health_def.resolved_key() if health_def else None
    if not dom_key:
        return _build_dom_placeholder_status(entry)

    if redis is None:
        return ServiceStatus(
            service=entry.service_id,
            status="unhealthy",
            detail="未提供 Redis 连接，无法加载 DOM 服务健康状态。",
        )

    payload = await _load_hash(redis, dom_key)
    if not payload:
        detail = (
            health_def.missing_detail
            if health_def and health_def.missing_detail
            else entry.external_detail
        )
        return ServiceStatus(
            service=entry.service_id,
            status="unhealthy",
            detail=detail,
        )

    return _build_status_from_payload(entry.service_id, payload)


async def _build_account_status(
    redis: Any | None,
    entry: ServiceRegistryEntry,
    readiness_payload: Mapping[str, Any] | None,
    readiness_status: str | None,
    readiness_detail: str | None,
) -> ServiceStatus:
    redis_key = _resolve_health_key(entry)
    payload = await _load_hash(redis, redis_key) if redis and redis_key else None
    fallback_detail = _missing_detail(entry, "账户服务未上报健康数据")

    if not payload:
        status = readiness_status or "unhealthy"
        detail = readiness_detail or fallback_detail
        payload_data: dict[str, Any] | None = None
        if readiness_payload is not None:
            payload_data = {"readiness": readiness_payload}
        return ServiceStatus(
            service=entry.service_id,
            status=status,
            detail=detail,
            payload=payload_data,
        )

    status_obj = _build_status_from_payload(entry.service_id, payload)
    if readiness_payload is not None:
        combined_payload: dict[str, Any] = dict(payload)
        combined_payload["readiness"] = readiness_payload
        status_obj.payload = combined_payload
    if readiness_status and readiness_status != "running":
        status_obj.status = readiness_status
        status_obj.detail = readiness_detail or status_obj.detail or fallback_detail
    elif readiness_detail and not status_obj.detail:
        status_obj.detail = readiness_detail
    return status_obj


async def gather_service_statuses(redis: Any | None = None) -> Sequence[ServiceStatus]:
    """Collect service statuses using Redis health hashes."""

    close_client = False
    client = redis
    if client is None:
        client = await get_redis_client()
        close_client = True

    account_readiness_payload: Mapping[str, Any] | None = None
    account_readiness_status: str | None = None
    account_readiness_detail: str | None = None

    try:
        readiness = await refresh_account_service_status(max_age=60.0, log_failure=False)
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Failed to refresh account service readiness: %s", exc)
        account_readiness_status = "unhealthy"
        account_readiness_detail = f"无法刷新账户服务状态: {exc}"
        account_readiness_payload = {"error": str(exc)}
    else:
        readiness_payload = readiness.as_payload()
        account_readiness_payload = readiness_payload
        if readiness.ready:
            account_readiness_status = "running"
            account_readiness_detail = "账户服务可用"
        elif readiness.checked_at is None:
            account_readiness_status = "unhealthy"
            account_readiness_detail = "账户服务尚未执行连通性检查"
        else:
            account_readiness_status = "unhealthy"
            account_readiness_detail = f"无法连接账户服务: {readiness.error or '未知错误'}"

    statuses: list[ServiceStatus] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for entry in SERVICE_REGISTRY:
        service_id = entry.service_id

        if service_id == "ibbot":
            statuses.append(
                ServiceStatus(
                    service=service_id,
                    status="running",
                    detail="主程序运行中",
                    last_reported=now_iso,
                )
            )
            continue

        if service_id == "ib_command":
            if client is None:
                statuses.append(
                    ServiceStatus(
                        service=service_id,
                        status="unhealthy",
                        detail="未提供 Redis 连接，无法加载健康信息。",
                    )
                )
            else:
                try:
                    statuses.append(await _build_ib_command_status(client))
                except Exception as exc:  # pragma: no cover - defensive guard
                    logger.warning("Failed to build ib_command health payload: %s", exc)
                    statuses.append(
                        ServiceStatus(
                            service=service_id,
                            status="unhealthy",
                            detail=f"无法加载健康信息: {exc}",
                        )
                    )
            continue

        if service_id == "account":
            statuses.append(
                await _build_account_status(
                    client,
                    entry,
                    account_readiness_payload,
                    account_readiness_status,
                    account_readiness_detail,
                )
            )
            continue

        if service_id == "queue_monitor":
            if client is None:
                statuses.append(await _build_queue_monitor_status(None, entry))
            else:
                try:
                    statuses.append(await _build_queue_monitor_status(client, entry))
                except Exception as exc:  # pragma: no cover - defensive guard
                    logger.warning("Failed to build queue monitor status: %s", exc)
                    statuses.append(
                        ServiceStatus(
                            service=service_id,
                            status="unhealthy",
                            detail=f"无法加载队列指标: {exc}",
                        )
                    )
            continue

        if service_id == DOM_SERVICE_NAME:
            try:
                statuses.append(await _build_dom_service_status(client, entry))
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.warning("Failed to build DOM service status: %s", exc)
                statuses.append(
                    ServiceStatus(
                        service=service_id,
                        status="unhealthy",
                        detail=f"无法加载 DOM 服务状态: {exc}",
                    )
                )
            continue

        redis_key = _resolve_health_key(entry)
        payload = await _load_hash(client, redis_key) if client and redis_key else None

        if payload:
            statuses.append(_build_status_from_payload(service_id, payload))
            continue

        if entry.internal:
            detail = _missing_detail(entry)
            statuses.append(
                ServiceStatus(
                    service=service_id,
                    status="unhealthy",
                    detail=detail,
                )
            )
        else:
            detail = entry.external_detail or "外部依赖"
            statuses.append(
                ServiceStatus(
                    service=service_id,
                    status="external" if service_id != DOM_SERVICE_NAME else "offline",
                    detail=detail,
                )
            )

    if close_client and client is not None:
        await _close_redis(client)

    return statuses


async def build_service_status_payload(redis: Any | None = None) -> Mapping[str, Any]:
    statuses = await gather_service_statuses(redis=redis)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "services": [status.as_mapping() for status in statuses],
        "registry": list(registry_as_mappings()),
    }
