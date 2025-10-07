"""Helpers for aggregating service health information."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, MutableMapping, Sequence

from loguru import logger

from src.account_service_readiness import refresh_account_service_status
from src.ib_command_health import (
    IB_COMMAND_HEALTH_KEY,
    IB_COMMAND_HEALTH_STALE_AFTER_SEC,
    build_ib_connection_health_payload,
)
from src.ibbot.redis import RedisError, get_redis_client
from src.routes.service_registry import SERVICE_REGISTRY
from src.service_restart import RestartMode, get_service_restart_manager
from src.startup_checks import get_startup_checks


__all__ = [
    "SERVICE_HEALTH_PROBES",
    "ServiceProbe",
    "ServiceStatus",
    "gather_service_statuses",
    "build_service_status_payload",
    "RedisUnavailableError",
]


@dataclass(frozen=True, slots=True)
class ServiceProbe:
    """Describe how to map a Redis health hash to a UI service card."""

    ui_service: str
    service_name: str
    default_key: str
    env_var: str | None = None
    missing_detail: str = "未上报健康数据"
    module_name: str | None = None

    @property
    def redis_key(self) -> str:
        if self.env_var:
            override = os.getenv(self.env_var, "").strip()
            if override:
                return override
        return self.default_key

    @property
    def module_id(self) -> str:
        if self.module_name:
            return self.module_name
        slug = self.service_name.replace("-", "_")
        return f"{slug}_probe"

def _build_service_probes() -> tuple[ServiceProbe, ...]:
    probes: list[ServiceProbe] = []
    for service in SERVICE_REGISTRY:
        if not service.internal:
            continue
        if service.slug in {"ibbot", "ib_command", "queue_monitor"}:
            continue
        default_key = service.default_health_key()
        if not default_key:
            continue
        missing_detail = service.missing_detail or f"{service.display_name} 未上报健康数据"
        probes.append(
            ServiceProbe(
                ui_service=service.slug,
                service_name=service.service_name or service.slug,
                default_key=default_key,
                env_var=service.health_env_var,
                missing_detail=missing_detail,
                module_name=service.module_name,
            )
        )
    return tuple(probes)


SERVICE_HEALTH_PROBES: tuple[ServiceProbe, ...] = _build_service_probes()

DOM_SERVICE_NAME = os.getenv("DOM_SERVICE_NAME", "dom-service")
DOM_SERVICE_HEALTH_KEY = (os.getenv("DOM_SERVICE_HEALTH_KEY", "") or "").strip()
QUEUE_MONITOR_HEALTH_HASH_KEY = os.getenv(
    "QUEUE_MONITOR_HEALTH_KEY", "hash:service:health:queue"
)
QUEUE_MONITOR_METRICS_HASH_KEY = os.getenv(
    "QUEUE_MONITOR_METRICS_HASH_KEY", "hash:service:queue-monitor"
)

_REDIS_COMMAND_TIMEOUT = float(os.getenv("SERVICE_STATUS_REDIS_TIMEOUT", "2.5"))
_REDIS_UNAVAILABLE_DETAIL = "Redis 不可用或命令超时"


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


EXTERNAL_SERVICES: dict[str, str] = {
    service.slug: service.description
    for service in SERVICE_REGISTRY
    if not service.internal
}


async def _close_redis(redis: Any) -> None:
    close = getattr(redis, "close", None)
    if close is None:
        return
    result = close()
    if asyncio.iscoroutine(result):
        try:
            await result
        except RuntimeError as exc:
            # Handle the case where event loop is already closed
            if "Event loop is closed" in str(exc):
                logger.debug("Event loop already closed during Redis cleanup")
            else:
                logger.debug(
                    "Ignoring redis.close() error during service status cleanup",
                    exc_info=True,
                )
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


_METRIC_FIELD_NAMES: tuple[str, ...] = (
    "last_refresh_requested",
    "account_snapshot_updated_at",
    "positions_snapshot_updated_at",
    "snapshot_updated_at",
    "stale",
    "stale_reason",
    "last_refresh_error",
)


def _coerce_metrics_mapping(
    payload: Mapping[str, Any],
    metric_keys: Iterable[str] = _METRIC_FIELD_NAMES,
) -> MutableMapping[str, Any]:
    """Return a mutable payload mapping with health metrics promoted."""

    data = dict(payload)
    existing_metrics = data.get("metrics")
    metrics_map: dict[str, Any]
    if isinstance(existing_metrics, Mapping):
        metrics_map = dict(existing_metrics)
    else:
        metrics_map = {}

    for key in metric_keys:
        if key not in data:
            continue
        value = data[key]
        if key == "stale":
            coerced = _coerce_bool(value)
            if coerced is not None:
                value = coerced
            data[key] = value
        if key not in metrics_map:
            metrics_map[key] = value

    if metrics_map:
        data["metrics"] = metrics_map

    return data


async def _load_hash(redis: Any, key: str) -> MutableMapping[str, Any] | None:
    try:
        values = await asyncio.wait_for(redis.hgetall(key), timeout=_REDIS_COMMAND_TIMEOUT)
    except asyncio.TimeoutError as exc:
        logger.warning("Redis command hgetall({}) timed out", key)
        raise RedisUnavailableError(f"Redis 命令超时: hgetall({key})") from exc
    except Exception as exc:  # pragma: no cover - depends on runtime redis availability
        logger.debug("Failed to load health hash {}: {}", key, exc)
        raise RedisUnavailableError(f"Redis 命令失败: hgetall({key})") from exc
    if not values:
        return None
    return dict(values)


def _build_redis_unavailable_status(
    service: str, *, reason: str | None = None, payload: Mapping[str, Any] | None = None
) -> ServiceStatus:
    detail = _REDIS_UNAVAILABLE_DETAIL
    reason_text = (reason or "").strip()
    if reason_text:
        detail = f"{detail}（{reason_text}）"
    payload_mapping: Mapping[str, Any] | None = None
    if payload is not None:
        payload_mapping = dict(payload)
    return ServiceStatus(
        service=service,
        status="unhealthy",
        detail=detail,
        payload=payload_mapping,
    )


def _build_status_from_payload(
    service: str, payload: Mapping[str, Any]
) -> ServiceStatus:
    prepared_payload = _coerce_metrics_mapping(payload)

    status_value = _normalize_status_value(
        prepared_payload.get("status") or prepared_payload.get("state")
    )
    healthy_flag = _coerce_bool(prepared_payload.get("healthy"))
    stale_flag = _coerce_bool(prepared_payload.get("stale"))
    last_error = prepared_payload.get("last_error") or prepared_payload.get("error")
    last_reported = _normalise_timestamp(
        prepared_payload.get("last_seen")
        or prepared_payload.get("checked_at")
        or prepared_payload.get("timestamp")
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
        payload=prepared_payload,
    )


async def _build_ib_command_status(redis: Any) -> ServiceStatus:
    try:
        raw = await _load_hash(redis, IB_COMMAND_HEALTH_KEY) or {}
    except RedisUnavailableError as exc:
        return _build_redis_unavailable_status("ib_command", reason=str(exc))
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


async def _build_queue_monitor_status(redis: Any) -> ServiceStatus:
    try:
        health_payload = await _load_hash(redis, QUEUE_MONITOR_HEALTH_HASH_KEY)
        snapshot_payload = await _load_hash(redis, QUEUE_MONITOR_METRICS_HASH_KEY)
    except RedisUnavailableError as exc:
        return _build_redis_unavailable_status("queue_monitor", reason=str(exc))

    if not health_payload and not snapshot_payload:
        return ServiceStatus(
            service="queue_monitor",
            status="unhealthy",
            detail="未上报队列指标，检查 queue-monitor 服务。",
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
        service="queue_monitor",
        status="running",
        detail=detail,
        last_reported=last_reported,
        payload=combined_payload or None,
    )


def _build_dom_placeholder_status() -> ServiceStatus:
    detail = "DOM 服务由外部部署提供（external/not provisioned），界面不会尝试自动启动。"
    return ServiceStatus(
        service=DOM_SERVICE_NAME,
        status="offline",
        detail=detail,
    )


async def _build_dom_service_status(redis: Any | None) -> ServiceStatus:
    if not DOM_SERVICE_HEALTH_KEY:
        return _build_dom_placeholder_status()

    if redis is None:
        return ServiceStatus(
            service=DOM_SERVICE_NAME,
            status="unhealthy",
            detail="未提供 Redis 连接，无法加载 DOM 服务健康状态。",
        )

    try:
        payload = await _load_hash(redis, DOM_SERVICE_HEALTH_KEY)
    except RedisUnavailableError as exc:
        return _build_redis_unavailable_status(DOM_SERVICE_NAME, reason=str(exc))
    if not payload:
        return ServiceStatus(
            service=DOM_SERVICE_NAME,
            status="unhealthy",
            detail="未上报 DOM 服务健康数据，检查外部 DOM 服务。",
        )

    return _build_status_from_payload(DOM_SERVICE_NAME, payload)


class RedisUnavailableError(RuntimeError):
    """Raised when Redis cannot be reached for service status aggregation."""


async def gather_service_statuses(redis: Any | None = None) -> Sequence[ServiceStatus]:
    """Collect service statuses using Redis health hashes."""

    close_client = False
    client = redis
    if client is None:
        try:
            client = await get_redis_client()
        except (RedisError, ConnectionError, asyncio.TimeoutError) as exc:
            logger.warning("Redis unavailable for service status collection", exc_info=True)
            raise RedisUnavailableError("Redis unavailable") from exc
        close_client = True

    statuses: list[ServiceStatus] = [
        ServiceStatus(
            service="ibbot",
            status="running",
            detail="主程序运行中",
            last_reported=datetime.now(timezone.utc).isoformat(),
        )
    ]

    try:
        statuses.append(await _build_ib_command_status(client))
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Failed to build ib_command health payload: {}", exc)
        statuses.append(
            ServiceStatus(
                service="ib_command",
                status="unhealthy",
                detail=f"无法加载健康信息: {exc}",
            )
        )

    account_readiness_payload: Mapping[str, Any] | None = None
    account_readiness_status: str | None = None
    account_readiness_detail: str | None = None

    try:
        readiness = await refresh_account_service_status(max_age=60.0, log_failure=False)
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Failed to refresh account service readiness: {}", exc)
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

    for probe in SERVICE_HEALTH_PROBES:
        redis_key = probe.redis_key
        if not redis_key:
            continue
        try:
            payload = await _load_hash(client, redis_key)
        except RedisUnavailableError as exc:
            if probe.ui_service == "account" and account_readiness_payload is not None:
                status_obj = _build_redis_unavailable_status(
                    probe.ui_service,
                    reason=str(exc),
                    payload={"readiness": account_readiness_payload},
                )
                if account_readiness_status and account_readiness_status != "running":
                    status_obj.status = account_readiness_status
                    extra_detail = account_readiness_detail or probe.missing_detail
                    if extra_detail:
                        if status_obj.detail:
                            status_obj.detail = f"{status_obj.detail}；{extra_detail}"
                        else:
                            status_obj.detail = extra_detail
                elif account_readiness_detail:
                    if status_obj.detail:
                        status_obj.detail = (
                            f"{status_obj.detail}；{account_readiness_detail}"
                        )
                    else:
                        status_obj.detail = account_readiness_detail
                statuses.append(status_obj)
            else:
                statuses.append(
                    _build_redis_unavailable_status(probe.ui_service, reason=str(exc))
                )
            continue
        if not payload:
            if probe.ui_service == "account" and account_readiness_payload is not None:
                statuses.append(
                    ServiceStatus(
                        service=probe.ui_service,
                        status=account_readiness_status or "unhealthy",
                        detail=account_readiness_detail or probe.missing_detail,
                        payload={"readiness": account_readiness_payload},
                    )
                )
            else:
                statuses.append(
                    ServiceStatus(
                        service=probe.ui_service,
                        status="unhealthy",
                        detail=probe.missing_detail,
                    )
                )
            continue

        status_obj = _build_status_from_payload(probe.ui_service, payload)
        if probe.ui_service == "account":
            combined_payload: dict[str, Any] = dict(status_obj.payload or payload)
            if account_readiness_payload is not None:
                combined_payload["readiness"] = account_readiness_payload
                status_obj.payload = combined_payload
            if account_readiness_status and account_readiness_status != "running":
                status_obj.status = account_readiness_status
                status_obj.detail = account_readiness_detail or status_obj.detail
            elif account_readiness_detail and not status_obj.detail:
                status_obj.detail = account_readiness_detail
        statuses.append(status_obj)

    try:
        statuses.append(await _build_queue_monitor_status(client))
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Failed to build queue monitor status: {}", exc)
        statuses.append(
            ServiceStatus(
                service="queue_monitor",
                status="unhealthy",
                detail=f"无法加载队列指标: {exc}",
            )
        )

    try:
        statuses.append(await _build_dom_service_status(client))
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Failed to build DOM service status: {}", exc)
        statuses.append(
            ServiceStatus(
                service=DOM_SERVICE_NAME,
                status="unhealthy",
                detail=f"无法加载 DOM 服务状态: {exc}",
            )
        )

    for service, message in EXTERNAL_SERVICES.items():
        statuses.append(
            ServiceStatus(
                service=service,
                status="external",
                detail=message,
            )
        )

    if close_client and client is not None:
        await _close_redis(client)

    return statuses


async def build_service_status_payload(redis: Any | None = None) -> Mapping[str, Any]:
    statuses = await gather_service_statuses(redis=redis)
    status_map = {status.service: status for status in statuses}
    services_payload: list[MutableMapping[str, Any]] = []
    seen: set[str] = set()

    for service in SERVICE_REGISTRY:
        seen.add(service.slug)
        status_obj = status_map.get(service.slug)
        if status_obj is None:
            detail = service.missing_detail or f"{service.display_name} 未上报健康数据"
            status_obj = ServiceStatus(
                service=service.slug,
                status="unhealthy" if service.internal else "external",
                detail=detail if service.internal else service.description,
            )
        data = status_obj.as_mapping()
        data["display_name"] = service.display_name
        data["description"] = service.description
        data["internal"] = service.internal
        data["service_name"] = service.service_name
        default_health_key = service.default_health_key()
        resolved_health_key = service.resolved_health_key()
        if default_health_key:
            data["default_health_key"] = default_health_key
        if resolved_health_key:
            data["health_key"] = resolved_health_key
        if service.port:
            data["port"] = service.port
        data["implementation_status"] = service.implementation_status
        services_payload.append(data)

    for status_obj in statuses:
        if status_obj.service in seen:
            continue
        data = status_obj.as_mapping()
        data.setdefault("display_name", status_obj.service)
        data.setdefault("internal", True)
        services_payload.append(data)

    manager = get_service_restart_manager()
    restart_mode = manager.mode
    if restart_mode is RestartMode.LOCAL:
        endpoint = "/services/restart/local"
    else:
        endpoint = "/services/restart"

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "services": services_payload,
        "restart_mode": restart_mode.value,
        "restart_endpoint": endpoint,
    }

    startup_checks = get_startup_checks()
    if startup_checks:
        payload["startup_checks"] = startup_checks

    return payload
