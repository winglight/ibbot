"""Central service registry shared between the API and frontend."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable, Tuple


@dataclass(frozen=True, slots=True)
class RegisteredService:
    """Describe a service that can appear in the status dashboard."""

    slug: str
    service_name: str
    display_name: str
    description: str
    internal: bool
    health_key: str | None = None
    health_env_var: str | None = None
    module_name: str | None = None
    missing_detail: str | None = None
    port: str | None = None
    implementation_status: str = "Production"

    def default_health_key(self) -> str | None:
        if self.health_key:
            return self.health_key
        if not self.service_name:
            return None
        return f"hash:service:health:{self.service_name}"

    def resolved_health_key(self) -> str | None:
        key = self.default_health_key()
        if self.health_env_var:
            override = os.getenv(self.health_env_var, "").strip()
            if override:
                key = override
        return key


def _registry() -> Tuple[RegisteredService, ...]:
    """Return the ordered list of registered services."""

    return (
        RegisteredService(
            slug="ibbot",
            service_name="ibbot",
            display_name="Trading Core",
            description="主应用 (ibbot)",
            internal=True,
            port="8000",
        ),
        RegisteredService(
            slug="ib_command",
            service_name="ib-command",
            display_name="Command Dispatcher",
            description="指令下发服务 (ib_command)",
            internal=True,
            health_key="hash:service:health:ib-command",
            health_env_var="IB_COMMAND_HEALTH_KEY",
            module_name="ib_command_probe",
            missing_detail="指令调度服务未上报健康数据",
            implementation_status="Production",
        ),
        RegisteredService(
            slug="connection_control",
            service_name="connection_control",
            display_name="Connection Control",
            description="IB 连接控制服务 (connection_control)",
            internal=True,
            missing_detail="连接控制服务未上报健康数据",
            port="8056",
        ),
        RegisteredService(
            slug="account",
            service_name="account-service",
            display_name="Account Service",
            description="账户服务 (account-service)",
            internal=True,
            health_env_var="ACCOUNT_SERVICE_HEALTH_KEY",
            module_name="account_service_probe",
            missing_detail="账户服务未上报健康数据",
            port="8020",
        ),
        RegisteredService(
            slug="orders",
            service_name="orders",
            display_name="Order Gateway",
            description="订单服务 (orders)",
            internal=True,
            health_env_var="ORDERS_SERVICE_HEALTH_KEY",
            module_name="orders_service_probe",
            missing_detail="订单服务未上报健康数据",
            port="8050",
        ),
        RegisteredService(
            slug="market_data",
            service_name="market-data",
            display_name="Market Data Sync",
            description="行情落地服务 (market_data)",
            internal=True,
            missing_detail="行情落地服务未上报健康数据",
            module_name="market_data_service_probe",
            port="8010",
            implementation_status="Production",
        ),
        RegisteredService(
            slug="risk",
            service_name="risk-service",
            display_name="Risk Manager",
            description="风险控制服务 (risk-service)",
            internal=True,
            health_env_var="RISK_SERVICE_HEALTH_KEY",
            module_name="risk_service_probe",
            port="8030",
        ),
        RegisteredService(
            slug="notification",
            service_name="notification",
            display_name="Notification Service",
            description="通知推送服务 (notification)",
            internal=True,
            health_key="hash:service:health:notification",
            health_env_var="NOTIFICATION_SERVICE_HEALTH_KEY",
            missing_detail="通知服务未上报健康数据",
            port="8060",
        ),
        RegisteredService(
            slug="strategy",
            service_name="strategy",
            display_name="Strategy Scheduler",
            description="策略调度服务 (strategy)",
            internal=True,
            health_env_var="STRATEGY_SERVICE_HEALTH_KEY",
            module_name="strategy_service_probe",
            port="8045",
        ),
        RegisteredService(
            slug="market_data_feed",
            service_name="ib-feed",
            display_name="Market Data Feed",
            description="行情订阅服务 (ib_feed)",
            internal=True,
            missing_detail="行情订阅服务未上报健康数据",
            module_name="ib_feed_service_probe",
            implementation_status="Beta",
        ),
        RegisteredService(
            slug="queue_monitor",
            service_name="queue-monitor",
            display_name="Queue Monitor",
            description="队列监控服务 (queue_monitor)",
            internal=True,
            health_key="hash:service:health:queue",
            health_env_var="QUEUE_MONITOR_HEALTH_KEY",
            module_name="queue_monitor_probe",
            missing_detail="队列监控服务未上报健康数据",
            port="8011",
        ),
    )


SERVICE_REGISTRY: Tuple[RegisteredService, ...] = _registry()


def iter_internal_services() -> Iterable[RegisteredService]:
    return (service for service in SERVICE_REGISTRY if service.internal)


__all__ = [
    "RegisteredService",
    "SERVICE_REGISTRY",
    "iter_internal_services",
]

