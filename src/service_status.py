"""Helpers for aggregating service health information."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, MutableMapping, Sequence

from loguru import logger

from src.ib_command_health import (
    IB_COMMAND_HEALTH_KEY,
    IB_COMMAND_HEALTH_STALE_AFTER_SEC,
    build_ib_connection_health_payload,
)
from src.ibbot.redis import get_redis_client


__all__ = [
    "SERVICE_HEALTH_PROBES",
    "ServiceStatus",
    "gather_service_statuses",
    "build_service_status_payload",
]


SERVICE_HEALTH_PROBES: tuple[tuple[str, str, str], ...] = (
    (
        "risk_service_probe",
        "risk-service",
        os.getenv("RISK_SERVICE_HEALTH_KEY", "hash:service:health:risk-service"),
    ),
    (
        "strategy_service_probe",
        "strategy",
        os.getenv("STRATEGY_SERVICE_HEALTH_KEY", "hash:service:health:strategy"),
    ),
    (
        "account_snapshot_probe",
        "account-snapshot",
        os.getenv(
            "ACCOUNT_SNAPSHOT_SERVICE_HEALTH_KEY",
            "hash:service:health:account-snapshot",
        ),
    ),
    (
        "queue_worker_probe",
        "queue-monitor",
        os.getenv("QUEUE_SERVICE_HEALTH_KEY", "hash:service:health:queue"),
    ),
)


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


_PLACEHOLDER_SERVICES: dict[str, str] = {
    "ib-gateway": "IB Gateway 状态未接入健康检查，假定运行中。",
    "ib_feed": "行情订阅服务未上报健康信息，默认显示运行中。",
    "optimizer": "Optimizer Engine 未提供健康接口，默认显示运行中。",
    "cloudflared": "Cloudflare Tunnel 状态未接入健康检查，默认显示运行中。",
    "notification": "通知服务未集成统一健康检查，默认显示运行中。",
    "market_data": "行情落地服务未接入健康检查，默认显示运行中。",
}

_SERVICE_ALIASES: dict[str, str] = {
    "queue-monitor": "queue_monitor",
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


async def _build_queue_monitor_status(redis: Any) -> ServiceStatus:
    payload = await _load_hash(redis, "hash:service:queue-monitor")
    if not payload:
        return ServiceStatus(
            service="queue_monitor",
            status="unhealthy",
            detail="未上报队列指标，检查 queue-monitor 服务。",
            last_reported=None,
            payload=None,
        )

    timestamp = _normalise_timestamp(payload.get("timestamp"))
    detail = "队列指标已更新"
    if timestamp:
        try:
            ts_dt = datetime.fromisoformat(timestamp)
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
                    last_reported=timestamp,
                    payload=payload,
                )
    return ServiceStatus(
        service="queue_monitor",
        status="running",
        detail=detail,
        last_reported=timestamp,
        payload=payload,
    )


async def gather_service_statuses(redis: Any | None = None) -> Sequence[ServiceStatus]:
    """Collect service statuses using Redis health hashes."""

    close_client = False
    client = redis
    if client is None:
        client = await get_redis_client()
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
        logger.warning("Failed to build ib_command health payload: %s", exc)
        statuses.append(
            ServiceStatus(
                service="ib_command",
                status="unhealthy",
                detail=f"无法加载健康信息: {exc}",
            )
        )

    for _, service_name, redis_key in SERVICE_HEALTH_PROBES:
        if not redis_key:
            continue
        ui_service = _SERVICE_ALIASES.get(service_name, service_name)
        payload = await _load_hash(client, redis_key)
        if not payload:
            statuses.append(
                ServiceStatus(
                    service=ui_service,
                    status="unhealthy",
                    detail="未上报健康数据",
                )
            )
            continue
        statuses.append(_build_status_from_payload(ui_service, payload))

    try:
        statuses.append(await _build_queue_monitor_status(client))
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Failed to build queue monitor status: %s", exc)
        statuses.append(
            ServiceStatus(
                service="queue_monitor",
                status="unhealthy",
                detail=f"无法加载队列指标: {exc}",
            )
        )

    for service, message in _PLACEHOLDER_SERVICES.items():
        statuses.append(
            ServiceStatus(
                service=service,
                status="running",
                detail=message,
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
    }
