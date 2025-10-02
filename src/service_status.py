"""Helpers for aggregating service health information."""

from __future__ import annotations

import asyncio
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
    DOM_SERVICE_HEALTH_KEY,
    DOM_SERVICE_NAME,
    QUEUE_MONITOR_METRICS_KEY,
    SERVICE_REGISTRY,
    SERVICE_REGISTRY_MAP,
    ServiceMetadata,
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


def _status_from_metadata(
    metadata: ServiceMetadata,
    *,
    status: str | None = None,
    detail: str | None = None,
) -> ServiceStatus:
    resolved_status = status or metadata.fallback_status or "unhealthy"
    resolved_detail = detail if detail is not None else metadata.fallback_detail
    if resolved_detail:
        resolved_detail = str(resolved_detail)
    else:
        resolved_detail = None
    return ServiceStatus(
        service=metadata.service,
        status=resolved_status,
        detail=resolved_detail,
    )


async def _build_ib_command_status(
    redis: Any | None, metadata: ServiceMetadata
) -> ServiceStatus:
    health_key = metadata.health_key or IB_COMMAND_HEALTH_KEY
    if redis is None or not health_key:
        return _status_from_metadata(
            metadata,
            status="unhealthy",
            detail=metadata.fallback_detail or "无法加载健康信息",
        )

    raw = await _load_hash(redis, health_key) or {}
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
    metadata: ServiceMetadata, redis: Any | None
) -> ServiceStatus:
    if redis is None:
        return _status_from_metadata(
            metadata,
            status="unhealthy",
            detail="未提供 Redis 连接，无法加载队列指标。",
        )

    metrics = await _load_hash(redis, QUEUE_MONITOR_METRICS_KEY)
    health_payload: Mapping[str, Any] | None = None
    if metadata.health_key:
        health_payload = await _load_hash(redis, metadata.health_key)

    if metrics:
        timestamp = _normalise_timestamp(metrics.get("timestamp"))
        detail = "队列指标已更新"
        if timestamp:
            try:
                ts_dt = datetime.fromisoformat(timestamp)
            except ValueError:
                ts_dt = None
            else:
                now = datetime.now(timezone.utc)
                if (now - ts_dt).total_seconds() > 60:
                    combined_payload = dict(health_payload or {})
                    combined_payload.update(metrics)
                    return ServiceStatus(
                        service=metadata.service,
                        status="unhealthy",
                        detail="队列指标超过 60 秒未更新",
                        last_reported=timestamp,
                        payload=combined_payload,
                    )
        combined_payload = dict(health_payload or {})
        combined_payload.update(metrics)
        return ServiceStatus(
            service=metadata.service,
            status="running",
            detail=detail,
            last_reported=timestamp,
            payload=combined_payload,
        )

    if health_payload:
        return _build_status_from_payload(metadata.service, health_payload)

    return _status_from_metadata(
        metadata,
        status=metadata.fallback_status or "unhealthy",
        detail=metadata.fallback_detail or "未上报队列指标，检查 queue-monitor 服务。",
    )


def _build_dom_placeholder_status(metadata: ServiceMetadata) -> ServiceStatus:
    return _status_from_metadata(
        metadata,
        status=metadata.fallback_status or "offline",
        detail=metadata.fallback_detail
        or "DOM 服务由外部部署提供（external/not provisioned），界面不会尝试自动启动。",
    )


async def _build_dom_service_status(
    metadata: ServiceMetadata, redis: Any | None
) -> ServiceStatus:
    if not metadata.health_key:
        return _build_dom_placeholder_status(metadata)

    if redis is None:
        return ServiceStatus(
            service=metadata.service,
            status="unhealthy",
            detail="未提供 Redis 连接，无法加载 DOM 服务健康状态。",
        )

    payload = await _load_hash(redis, metadata.health_key)
    if not payload:
        return _status_from_metadata(
            metadata,
            status=metadata.fallback_status or "unhealthy",
            detail=metadata.fallback_detail
            or "未上报 DOM 服务健康数据，检查外部 DOM 服务。",
        )

    return _build_status_from_payload(metadata.service, payload)


async def _build_account_service_status(
    metadata: ServiceMetadata,
) -> ServiceStatus:
    try:
        readiness = await refresh_account_service_status(
            max_age=60.0, log_failure=False
        )
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Failed to refresh account service readiness: %s", exc)
        return ServiceStatus(
            service=metadata.service,
            status="unhealthy",
            detail=f"无法刷新账户服务状态: {exc}",
        )

    readiness_payload = readiness.as_payload()
    checked_at = readiness_payload.get("checked_at")
    if readiness.ready:
        detail = "账户服务可用"
        status = "running"
    elif readiness.checked_at is None:
        detail = metadata.fallback_detail or "账户服务尚未执行连通性检查"
        status = "unhealthy"
    else:
        detail = f"无法连接账户服务: {readiness.error or '未知错误'}"
        status = "unhealthy"

    return ServiceStatus(
        service=metadata.service,
        status=status,
        detail=detail,
        last_reported=checked_at,
        payload=readiness_payload,
    )


async def gather_service_statuses(redis: Any | None = None) -> Sequence[ServiceStatus]:
    """Collect service statuses using Redis health hashes."""

    close_client = False
    client = redis
    if client is None:
        try:
            client = await get_redis_client()
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.warning("Failed to acquire Redis client for service status: %s", exc)
            client = None
        else:
            close_client = True

    statuses: dict[str, ServiceStatus] = {}

    ibbot_meta = SERVICE_REGISTRY_MAP.get("ibbot")
    base_detail = ibbot_meta.fallback_detail if ibbot_meta else None
    statuses["ibbot"] = ServiceStatus(
        service="ibbot",
        status="running",
        detail=base_detail or "主程序运行中",
        last_reported=datetime.now(timezone.utc).isoformat(),
    )

    for entry in SERVICE_REGISTRY:
        service = entry.service
        if service == "ibbot":
            continue

        if service == "ib_command":
            try:
                statuses[service] = await _build_ib_command_status(client, entry)
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.warning("Failed to build ib_command health payload: %s", exc)
                statuses[service] = ServiceStatus(
                    service=service,
                    status="unhealthy",
                    detail=f"无法加载健康信息: {exc}",
                )
            continue

        if service == "account-service":
            statuses[service] = await _build_account_service_status(entry)
            continue

        if service == "queue_monitor":
            try:
                statuses[service] = await _build_queue_monitor_status(entry, client)
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.warning("Failed to build queue monitor status: %s", exc)
                statuses[service] = ServiceStatus(
                    service=service,
                    status="unhealthy",
                    detail=f"无法加载队列指标: {exc}",
                )
            continue

        if service == DOM_SERVICE_NAME:
            try:
                statuses[service] = await _build_dom_service_status(entry, client)
            except Exception as exc:  # pragma: no cover - defensive guard
                logger.warning("Failed to build DOM service status: %s", exc)
                statuses[service] = ServiceStatus(
                    service=service,
                    status="unhealthy",
                    detail=f"无法加载 DOM 服务状态: {exc}",
                )
            continue

        if entry.health_key:
            if client is None:
                statuses[service] = _status_from_metadata(
                    entry,
                    status=entry.fallback_status or "unhealthy",
                    detail=entry.fallback_detail or "无法加载健康信息",
                )
                continue

            payload = await _load_hash(client, entry.health_key)
            if not payload:
                statuses[service] = _status_from_metadata(
                    entry,
                    status=entry.fallback_status or "unhealthy",
                    detail=entry.fallback_detail or "未上报健康数据",
                )
            else:
                statuses[service] = _build_status_from_payload(service, payload)
            continue

        statuses[service] = _status_from_metadata(entry)

    if close_client and client is not None:
        await _close_redis(client)

    ordered: list[ServiceStatus] = []
    seen: set[str] = set()
    for entry in SERVICE_REGISTRY:
        status = statuses.get(entry.service)
        if status is None:
            status = _status_from_metadata(entry)
        ordered.append(status)
        seen.add(entry.service)

    for service, status in statuses.items():
        if service not in seen:
            ordered.append(status)

    return ordered


async def build_service_status_payload(redis: Any | None = None) -> Mapping[str, Any]:
    statuses = await gather_service_statuses(redis=redis)
    services: list[dict[str, Any]] = []
    for status in statuses:
        entry = status.as_mapping()
        metadata = SERVICE_REGISTRY_MAP.get(status.service)
        if metadata is not None:
            entry["label"] = metadata.display_name
            entry["description"] = metadata.description
            entry["internal"] = metadata.internal
            if metadata.health_key:
                entry["health_key"] = metadata.health_key
            if metadata.ports:
                entry["ports"] = list(metadata.ports)
        services.append(entry)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "services": services,
    }
