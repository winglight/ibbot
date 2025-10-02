"""Central service registry shared between API and UI layers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Sequence

from src.ib_command_health import IB_COMMAND_HEALTH_KEY


@dataclass(frozen=True, slots=True)
class ServiceHealthDefinition:
    """Describe how a service reports its health to Redis."""

    default_key: str | None
    env_var: str | None = None
    missing_detail: str | None = None

    def resolved_key(self) -> str | None:
        if self.env_var:
            override = os.getenv(self.env_var, "").strip()
            if override:
                return override
        return self.default_key


@dataclass(frozen=True, slots=True)
class ServiceRegistryEntry:
    """Metadata describing an individual infrastructure service."""

    service_id: str
    display_name: str
    description: str
    internal: bool
    health: ServiceHealthDefinition | None = None
    restart_service: str | None = None
    port: int | None = None
    external_detail: str | None = None

    def as_mapping(self) -> Mapping[str, object | None]:
        return {
            "service": self.service_id,
            "display_name": self.display_name,
            "description": self.description,
            "internal": self.internal,
            "restart_service": self.restart_service,
            "port": self.port,
            "health_key": self.health.resolved_key() if self.health else None,
        }


DOM_SERVICE_NAME = os.getenv("DOM_SERVICE_NAME", "dom-service")
_DOM_HEALTH_OVERRIDE = (os.getenv("DOM_SERVICE_HEALTH_KEY", "") or "").strip()


def _dom_service_health() -> ServiceHealthDefinition | None:
    if not _DOM_HEALTH_OVERRIDE:
        return None
    return ServiceHealthDefinition(
        default_key=_DOM_HEALTH_OVERRIDE,
        env_var="DOM_SERVICE_HEALTH_KEY",
        missing_detail="未上报 DOM 服务健康数据，检查外部 DOM 服务。",
    )


_BASE_SERVICE_REGISTRY: Sequence[ServiceRegistryEntry] = (
    ServiceRegistryEntry(
        service_id="ib-gateway",
        display_name="IB Gateway",
        description="交互式经纪商终端",
        internal=False,
        restart_service="ib-gateway",
        port=4002,
        external_detail="IB Gateway 由外部部署提供，需手动确认运行状态。",
    ),
    ServiceRegistryEntry(
        service_id="ibbot",
        display_name="Trading Core",
        description="主应用 (ibbot)",
        internal=True,
        restart_service="ibbot",
        port=8000,
    ),
    ServiceRegistryEntry(
        service_id="ib_command",
        display_name="Command Dispatcher",
        description="指令下发服务 (ib_command)",
        internal=True,
        health=ServiceHealthDefinition(
            default_key=IB_COMMAND_HEALTH_KEY,
            env_var="IB_COMMAND_HEALTH_KEY",
            missing_detail="指令下发服务未上报健康数据",
        ),
        restart_service="ib-command",
    ),
    ServiceRegistryEntry(
        service_id="account",
        display_name="Account Service",
        description="账户服务 (account-service)",
        internal=True,
        health=ServiceHealthDefinition(
            default_key="hash:service:health:account-service",
            env_var="ACCOUNT_SERVICE_HEALTH_KEY",
            missing_detail="账户服务未上报健康数据",
        ),
        restart_service="account",
        port=8020,
    ),
    ServiceRegistryEntry(
        service_id="orders",
        display_name="Order Orchestrator",
        description="订单管理服务 (orders)",
        internal=True,
        health=ServiceHealthDefinition(
            default_key="hash:service:health:orders",
            env_var="ORDERS_SERVICE_HEALTH_KEY",
            missing_detail="订单服务未上报健康数据",
        ),
    ),
    ServiceRegistryEntry(
        service_id="risk",
        display_name="Risk Manager",
        description="风险控制服务 (risk-service)",
        internal=True,
        health=ServiceHealthDefinition(
            default_key="hash:service:health:risk-service",
            env_var="RISK_SERVICE_HEALTH_KEY",
            missing_detail="风险控制服务未上报健康数据",
        ),
    ),
    ServiceRegistryEntry(
        service_id="strategy",
        display_name="Strategy Scheduler",
        description="策略调度服务 (strategy)",
        internal=True,
        health=ServiceHealthDefinition(
            default_key="hash:service:health:strategy",
            env_var="STRATEGY_SERVICE_HEALTH_KEY",
            missing_detail="策略调度服务未上报健康数据",
        ),
    ),
    ServiceRegistryEntry(
        service_id="notification",
        display_name="Notification Relay",
        description="通知推送服务 (notification)",
        internal=True,
        health=ServiceHealthDefinition(
            default_key="hash:service:health:notification",
            env_var="NOTIFICATION_SERVICE_HEALTH_KEY",
            missing_detail="通知服务未上报健康数据",
        ),
    ),
    ServiceRegistryEntry(
        service_id="account_snapshot",
        display_name="Account Snapshot",
        description="账户快照服务 (account-snapshot)",
        internal=True,
        health=ServiceHealthDefinition(
            default_key="hash:service:health:account-snapshot",
            env_var="ACCOUNT_SNAPSHOT_SERVICE_HEALTH_KEY",
            missing_detail="账户快照服务未上报健康数据",
        ),
    ),
    ServiceRegistryEntry(
        service_id="ib_feed",
        display_name="Market Data Feed",
        description="行情订阅服务 (ib_feed)",
        internal=True,
        health=ServiceHealthDefinition(
            default_key="hash:service:health:ib-feed",
            env_var="IB_FEED_SERVICE_HEALTH_KEY",
            missing_detail="行情订阅服务未上报健康数据",
        ),
    ),
    ServiceRegistryEntry(
        service_id="market_data",
        display_name="Market Data Sync",
        description="行情落地服务 (market_data)",
        internal=True,
        health=ServiceHealthDefinition(
            default_key="hash:service:health:market-data",
            env_var="MARKET_DATA_SERVICE_HEALTH_KEY",
            missing_detail="行情落地服务未上报健康数据",
        ),
        port=8010,
    ),
    ServiceRegistryEntry(
        service_id="queue_monitor",
        display_name="Queue Monitor",
        description="队列监控服务 (queue_monitor)",
        internal=True,
        health=ServiceHealthDefinition(
            default_key="hash:service:health:queue",
            env_var="QUEUE_MONITOR_HEALTH_KEY",
            missing_detail="队列监控服务未上报健康数据",
        ),
        port=8011,
    ),
    ServiceRegistryEntry(
        service_id="optimizer",
        display_name="Optimizer Engine",
        description="参数优化服务 (optimizer)",
        internal=True,
        health=ServiceHealthDefinition(
            default_key="hash:service:health:optimizer",
            env_var="OPTIMIZER_SERVICE_HEALTH_KEY",
            missing_detail="优化器未上报健康数据",
        ),
        restart_service="optimizer",
        port=8001,
    ),
    ServiceRegistryEntry(
        service_id="cloudflared",
        display_name="Cloudflare Tunnel",
        description="Cloudflare 隧道 (cloudflared)",
        internal=False,
        restart_service="cloudflared",
        external_detail="Cloudflared 隧道为外部依赖，请使用独立监控确认状态。",
    ),
)


def build_service_registry() -> tuple[ServiceRegistryEntry, ...]:
    """Return the ordered service registry, including dynamic entries."""

    entries: list[ServiceRegistryEntry] = list(_BASE_SERVICE_REGISTRY)
    dom_entry = ServiceRegistryEntry(
        service_id=DOM_SERVICE_NAME,
        display_name="DOM 数据服务",
        description="外部 DOM 指标/行情服务",
        internal=False,
        health=_dom_service_health(),
        external_detail="DOM 服务由外部部署提供（external/not provisioned），界面不会尝试自动启动。",
    )
    entries.append(dom_entry)
    return tuple(entries)


SERVICE_REGISTRY: tuple[ServiceRegistryEntry, ...] = build_service_registry()


def registry_as_mappings() -> tuple[Mapping[str, object | None], ...]:
    return tuple(entry.as_mapping() for entry in SERVICE_REGISTRY)


__all__ = [
    "SERVICE_REGISTRY",
    "ServiceRegistryEntry",
    "ServiceHealthDefinition",
    "DOM_SERVICE_NAME",
    "registry_as_mappings",
]
