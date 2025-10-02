from __future__ import annotations

"""Central registry describing managed and auxiliary services."""

import os
from dataclasses import dataclass
from typing import Iterable, Mapping


__all__ = [
    "ServiceMetadata",
    "SERVICE_REGISTRY",
    "SERVICE_REGISTRY_MAP",
    "DOM_SERVICE_NAME",
    "DOM_SERVICE_HEALTH_KEY",
    "QUEUE_MONITOR_METRICS_KEY",
    "iter_service_registry",
]


@dataclass(frozen=True, slots=True)
class ServiceMetadata:
    """Metadata describing a service surfaced in the status dashboard."""

    service: str
    display_name: str
    description: str
    health_key: str | None
    internal: bool
    ports: tuple[int, ...] = ()
    fallback_status: str = "unhealthy"
    fallback_detail: str | None = None


def _env_text(name: str, default: str = "") -> str:
    value = os.getenv(name, default)
    if value is None:
        return default
    text = str(value).strip()
    return text or default


DOM_SERVICE_NAME = _env_text("DOM_SERVICE_NAME", "dom-service")
DOM_SERVICE_HEALTH_KEY = _env_text("DOM_SERVICE_HEALTH_KEY", "") or None
QUEUE_MONITOR_METRICS_KEY = "hash:service:queue-monitor"


def _health_key_from_env(name: str, default: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    if not value:
        return None
    return value


_SERVICE_REGISTRY: list[ServiceMetadata] = [
    ServiceMetadata(
        service="ibbot",
        display_name="Trading Core",
        description="主应用 (ibbot)",
        health_key=None,
        internal=True,
        ports=(8000,),
        fallback_status="running",
        fallback_detail="主程序运行中",
    ),
    ServiceMetadata(
        service="ib_command",
        display_name="Command Dispatcher",
        description="指令下发服务 (ib_command)",
        health_key=_health_key_from_env(
            "IB_COMMAND_HEALTH_KEY",
            "hash:service:health:ib-command",
        ),
        internal=True,
    ),
    ServiceMetadata(
        service="ib_feed",
        display_name="Market Data Feed",
        description="行情订阅服务 (ib_feed)",
        health_key=None,
        internal=True,
        fallback_status="running",
        fallback_detail="行情订阅服务未上报健康信息，默认显示运行中。",
    ),
    ServiceMetadata(
        service="risk-service",
        display_name="Risk Manager",
        description="风险控制服务 (risk-service)",
        health_key=_health_key_from_env(
            "RISK_SERVICE_HEALTH_KEY",
            "hash:service:health:risk-service",
        ),
        internal=True,
    ),
    ServiceMetadata(
        service="strategy",
        display_name="Strategy Scheduler",
        description="策略调度服务 (strategy)",
        health_key=_health_key_from_env(
            "STRATEGY_SERVICE_HEALTH_KEY",
            "hash:service:health:strategy",
        ),
        internal=True,
    ),
    ServiceMetadata(
        service="account-service",
        display_name="Account Service API",
        description="账户服务 (account-service)",
        health_key=None,
        internal=True,
        ports=(8020,),
        fallback_status="unhealthy",
        fallback_detail="账户服务尚未执行连通性检查",
    ),
    ServiceMetadata(
        service="account-snapshot",
        display_name="Account Snapshot Refresher",
        description="账户快照服务 (account-snapshot)",
        health_key=_health_key_from_env(
            "ACCOUNT_SNAPSHOT_SERVICE_HEALTH_KEY",
            "hash:service:health:account-snapshot",
        ),
        internal=True,
    ),
    ServiceMetadata(
        service="queue_monitor",
        display_name="Queue Monitor",
        description="队列监控服务 (queue_monitor)",
        health_key=_health_key_from_env(
            "QUEUE_SERVICE_HEALTH_KEY",
            "hash:service:health:queue",
        ),
        internal=True,
        ports=(8011,),
    ),
    ServiceMetadata(
        service="market_data",
        display_name="Market Data Sync",
        description="行情落地服务 (market_data)",
        health_key=None,
        internal=True,
        ports=(8010,),
        fallback_status="running",
        fallback_detail="行情落地服务未接入健康检查，默认显示运行中。",
    ),
    ServiceMetadata(
        service="notification",
        display_name="Notification Relay",
        description="通知推送服务 (notification)",
        health_key=None,
        internal=True,
        fallback_status="running",
        fallback_detail="通知服务未集成统一健康检查，默认显示运行中。",
    ),
    ServiceMetadata(
        service="optimizer",
        display_name="Optimizer Engine",
        description="策略优化引擎 (optimizer)",
        health_key=None,
        internal=True,
        ports=(8001,),
        fallback_status="running",
        fallback_detail="Optimizer Engine 未提供健康接口，默认显示运行中。",
    ),
    ServiceMetadata(
        service="ib-gateway",
        display_name="IB Gateway",
        description="IBKR 官方终端 (ib-gateway)",
        health_key=None,
        internal=False,
        ports=(4001, 4002, 5800, 5900),
        fallback_status="running",
        fallback_detail="IB Gateway 状态未接入健康检查，假定运行中。",
    ),
    ServiceMetadata(
        service="cloudflared",
        display_name="Cloudflare Tunnel",
        description="Cloudflare 隧道 (cloudflared)",
        health_key=None,
        internal=False,
        fallback_status="running",
        fallback_detail="Cloudflare Tunnel 状态未接入健康检查，默认显示运行中。",
    ),
]

_dom_fallback_status = "unhealthy"
_dom_fallback_detail = "未上报 DOM 服务健康数据，检查外部 DOM 服务。"
if DOM_SERVICE_HEALTH_KEY is None:
    _dom_fallback_status = "offline"
    _dom_fallback_detail = (
        "DOM 服务由外部部署提供（external/not provisioned），界面不会尝试自动启动。"
    )

_SERVICE_REGISTRY.append(
    ServiceMetadata(
        service=DOM_SERVICE_NAME,
        display_name="DOM Bridge",
        description=f"DOM 行情桥接服务 ({DOM_SERVICE_NAME})",
        health_key=DOM_SERVICE_HEALTH_KEY,
        internal=False,
        fallback_status=_dom_fallback_status,
        fallback_detail=_dom_fallback_detail,
    )
)

SERVICE_REGISTRY: tuple[ServiceMetadata, ...] = tuple(_SERVICE_REGISTRY)
SERVICE_REGISTRY_MAP: Mapping[str, ServiceMetadata] = {
    entry.service: entry for entry in SERVICE_REGISTRY
}


def iter_service_registry() -> Iterable[ServiceMetadata]:
    """Return an iterable over the registered services."""

    return iter(SERVICE_REGISTRY)
