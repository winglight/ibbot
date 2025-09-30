"""Application-wide service container initialised during FastAPI startup."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

import httpx
from loguru import logger

from src.account_snapshot_service import AccountSnapshotService
from src.atr_trailing_service import ATRTrailingService
from src.cache import init_redis
from src.config import load_project_env
from src.database import DatabaseManager, get_db_manager
from src.dom_dependencies import (
    DOMConfig,
    depth_subscriber as shared_depth_subscriber,
    dom_config as shared_dom_config,
    get_dom_processor,
)
from src.dom_service import get_dom_service
from src.ibbot.config import ConfigProvider
from src.ibbot.influx import InfluxConfig, configure_influx
from src.ibkr_client import IBKRClient
from src.market_data_stream import PriceVolumeStream
from src.notification_manager import NotificationManager
from src.order_manager import OrderManager, get_order_manager
from src.queue_manager import queue_manager
from src.risk_manager import RiskManager
from src.strategy_manager import StrategyManager
from src.strategy_risk import StrategyRiskManager
from src.strategies.buy_the_dip_strategy import BuyTheDipStrategy
from src.strategies.dom_strategy import DOMStrategy
from src.telegram_notifier import TelegramNotifier
from src.utils import now_local
from src.websocket_manager import WebSocketManager, websocket_manager


def _parse_endpoint_mapping(raw: Any) -> dict[str, str]:
    """Normalise *raw* payload into a mapping of module -> health URL."""

    if not raw:
        return {}

    if isinstance(raw, Mapping):
        return {str(k).strip(): str(v).strip() for k, v in raw.items() if v}

    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        else:
            if isinstance(parsed, Mapping):
                return _parse_endpoint_mapping(parsed)
            if isinstance(parsed, list):
                mapping: dict[str, str] = {}
                for item in parsed:
                    mapping.update(_parse_endpoint_mapping(item))
                return mapping

        tokens = [token.strip() for token in text.replace("\n", ",").split(",") if token.strip()]
        mapping: dict[str, str] = {}
        for token in tokens:
            if "=" in token:
                module, url = token.split("=", 1)
            elif ":" in token:
                module, url = token.split(":", 1)
            else:
                continue
            module = module.strip()
            url = url.strip()
            if module and url:
                mapping[module] = url
        return mapping

    if isinstance(raw, (list, tuple, set)):
        mapping: dict[str, str] = {}
        for item in raw:
            mapping.update(_parse_endpoint_mapping(item))
        return mapping

    return {}


async def _resolve_health_endpoints(provider: ConfigProvider | None) -> dict[str, str]:
    """Resolve service health endpoints from configuration sources."""

    candidate: Any = None
    if provider is not None:
        try:
            services_config = await provider.get_module("services")
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.warning("Failed to load services module config: {}", exc)
            services_config = {}
        candidate = (
            services_config.get("health_endpoints")
            or services_config.get("service_health_endpoints")
        )

    if not candidate:
        for key in ("IBBOT_SERVICE_HEALTH_ENDPOINTS", "SERVICE_HEALTH_ENDPOINTS"):
            env_value = os.getenv(key)
            if env_value:
                candidate = env_value
                break

    return _parse_endpoint_mapping(candidate)


@dataclass(slots=True)
class HealthResult:
    """Outcome of an HTTP health probe."""

    module: str
    healthy: bool
    status_code: Optional[int]
    latency_ms: float
    response: Any = None
    error: Optional[str] = None

    def as_redis_mapping(self) -> Dict[str, str]:
        payload: Dict[str, str] = {
            "status": "up" if self.healthy else "down",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "latency_ms": f"{self.latency_ms:.2f}",
        }
        if self.status_code is not None:
            payload["status_code"] = str(self.status_code)
        if self.error:
            payload["error"] = self.error
        if self.response is not None:
            if isinstance(self.response, str):
                payload["response"] = self.response
            else:
                try:
                    payload["response"] = json.dumps(self.response)
                except TypeError:
                    payload["response"] = str(self.response)
        return payload


async def _probe_health(module: str, url: str) -> HealthResult:
    """Execute a single HTTP health probe."""

    start = asyncio.get_event_loop().time()
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=5.0)
    except Exception as exc:
        latency_ms = (asyncio.get_event_loop().time() - start) * 1000.0
        logger.warning("Health probe failed", module=module, url=url, error=str(exc))
        return HealthResult(
            module=module,
            healthy=False,
            status_code=None,
            latency_ms=latency_ms,
            error=str(exc),
        )

    latency_ms = (asyncio.get_event_loop().time() - start) * 1000.0
    body: Any
    healthy = response.status_code < 400
    try:
        body = response.json()
    except ValueError:
        body = response.text
    else:
        if isinstance(body, Mapping):
            healthy = bool(body.get("healthy", healthy))

    return HealthResult(
        module=module,
        healthy=healthy,
        status_code=response.status_code,
        latency_ms=latency_ms,
        response=body,
    )


@dataclass(slots=True)
class CoreContainer:
    """Shared application state accessible throughout the FastAPI app."""

    telegram_notifier: TelegramNotifier
    db_manager: DatabaseManager
    notification_manager: NotificationManager
    risk_manager: RiskManager
    strategy_manager: StrategyManager
    strategy_risk_manager: StrategyRiskManager
    queue_manager: Any
    websocket_manager: WebSocketManager
    order_manager: OrderManager
    account_snapshot_service: AccountSnapshotService
    price_volume_stream: PriceVolumeStream
    atr_trailing_service: ATRTrailingService
    dom_config: DOMConfig
    dom_processor: Any
    depth_subscriber: Any
    redis_client: Any | None
    influx_config: InfluxConfig | None
    config_provider: ConfigProvider | None
    service_health_endpoints: dict[str, str] = field(default_factory=dict)
    ibkr_client: IBKRClient | None = None

    async def run_health_checks(self) -> Dict[str, HealthResult]:
        """Run health checks for configured services and persist to Redis."""

        if not self.service_health_endpoints:
            return {}

        results: Dict[str, HealthResult] = {}
        redis_client = self.redis_client
        for module, url in self.service_health_endpoints.items():
            result = await _probe_health(module, url)
            results[module] = result
            if redis_client is None:
                continue
            try:
                await redis_client.hset(
                    f"hash:service:health:{module}",
                    mapping=result.as_redis_mapping(),
                )
            except Exception as exc:  # pragma: no cover - redis failure guard
                logger.warning(
                    "Failed to record health result in Redis",
                    module=module,
                    error=str(exc),
                )
        return results


_container: CoreContainer | None = None


def get_container() -> CoreContainer | None:
    """Return the currently initialised container, if any."""

    return _container


def require_container() -> CoreContainer:
    """Return the active container or raise a runtime error."""

    if _container is None:
        raise RuntimeError("Core container has not been initialised")
    return _container


def set_container(container: CoreContainer) -> CoreContainer:
    """Set the active container (primarily used by the startup hook)."""

    global _container
    _container = container
    return container


async def initialise(*, force: bool = False) -> CoreContainer:
    """Initialise shared services and return the application container."""

    if _container is not None and not force:
        return _container

    load_project_env()
    logger.info("Initialising application container at {}", now_local())

    redis_client = await init_redis()
    influx_config: InfluxConfig | None = None
    try:
        influx_config = configure_influx()
    except Exception as exc:  # pragma: no cover - optional dependency errors
        logger.warning("Influx configuration failed: {}", exc)

    telegram_notifier = TelegramNotifier()
    db_manager = get_db_manager()
    notification_manager = NotificationManager(
        telegram_notifier=telegram_notifier,
        websocket_manager=websocket_manager,
        db_manager=db_manager,
    )

    risk_manager = RiskManager(notification_manager=notification_manager)
    strategy_manager = StrategyManager()
    strategy_risk_manager = StrategyRiskManager(
        db_manager=db_manager,
        notification_manager=notification_manager,
    )

    price_volume_stream = PriceVolumeStream()
    atr_trailing_service = ATRTrailingService(
        price_stream=price_volume_stream,
        risk_manager=risk_manager,
        notification_manager=notification_manager,
    )

    order_manager = get_order_manager()
    account_snapshot_service = AccountSnapshotService()

    try:
        strategy_manager.set_dependencies(
            risk_manager=risk_manager,
            queue_manager=queue_manager,
            notification_manager=notification_manager,
            strategy_risk_manager=strategy_risk_manager,
        )
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Failed to set StrategyManager dependencies: {}", exc)

    try:
        order_manager.set_dependencies(
            websocket_manager=websocket_manager,
            notification_manager=notification_manager,
            risk_manager=risk_manager,
        )
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Failed to set OrderManager dependencies: {}", exc)

    try:
        risk_manager.set_dependencies(
            order_manager=order_manager,
            atr_service=atr_trailing_service,
        )
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Failed to set RiskManager dependencies: {}", exc)

    try:
        queue_manager.set_dependencies(
            risk_manager=risk_manager,
            strategy_manager=strategy_manager,
            websocket_manager=websocket_manager,
            order_manager=order_manager,
            notification_manager=notification_manager,
        )
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Failed to set QueueManager dependencies: {}", exc)

    try:
        strategy_risk_manager.set_dependencies(
            strategy_manager=strategy_manager,
            queue_manager=queue_manager,
            notification_manager=notification_manager,
        )
        strategy_risk_manager.start()
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Failed to start StrategyRiskManager: {}", exc)

    try:
        dom_strategy = DOMStrategy()
        dom_strategy.set_dependencies(
            dom_service=get_dom_service(),
            strategy_manager=strategy_manager,
            order_manager=order_manager,
            queue_manager=queue_manager,
            risk_manager=risk_manager,
            strategy_risk_manager=strategy_risk_manager,
            notification_manager=notification_manager,
        )
        strategy_manager.register_internal_strategy(dom_strategy)
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Failed to initialise DOM strategy: {}", exc)

    try:
        btd_strategy = BuyTheDipStrategy()
        btd_strategy.set_dependencies(
            strategy_manager=strategy_manager,
            order_manager=order_manager,
            queue_manager=queue_manager,
            risk_manager=risk_manager,
            strategy_risk_manager=strategy_risk_manager,
            notification_manager=notification_manager,
        )
        strategy_manager.register_internal_strategy(btd_strategy)
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Failed to initialise Buy-the-Dip strategy: {}", exc)

    try:
        strategy_manager.rebuild_strategy_cache(block=False)
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Failed to schedule strategy cache rebuild: {}", exc)

    config_provider: ConfigProvider | None = None
    try:
        config_provider = ConfigProvider(redis_client=redis_client)
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("ConfigProvider initialisation failed: {}", exc)
        config_provider = None

    if config_provider is not None:
        try:
            await config_provider.start()
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.warning("Failed to start ConfigProvider listener: {}", exc)

    service_health_endpoints = await _resolve_health_endpoints(config_provider)

    container = CoreContainer(
        telegram_notifier=telegram_notifier,
        db_manager=db_manager,
        notification_manager=notification_manager,
        risk_manager=risk_manager,
        strategy_manager=strategy_manager,
        strategy_risk_manager=strategy_risk_manager,
        queue_manager=queue_manager,
        websocket_manager=websocket_manager,
        order_manager=order_manager,
        account_snapshot_service=account_snapshot_service,
        price_volume_stream=price_volume_stream,
        atr_trailing_service=atr_trailing_service,
        dom_config=shared_dom_config,
        dom_processor=get_dom_processor(),
        depth_subscriber=shared_depth_subscriber,
        redis_client=redis_client,
        influx_config=influx_config,
        config_provider=config_provider,
        service_health_endpoints=service_health_endpoints,
    )

    set_container(container)
    return container


async def shutdown() -> None:
    """Release resources managed by the container."""

    global _container
    container = _container
    _container = None
    if container is None:
        return

    if container.config_provider is not None:
        try:
            await container.config_provider.close()
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.warning("Error while closing ConfigProvider: {}", exc)


__all__ = [
    "AccountSnapshotService",
    "ATRTrailingService",
    "ConfigProvider",
    "CoreContainer",
    "IBKRClient",
    "NotificationManager",
    "PriceVolumeStream",
    "RiskManager",
    "StrategyManager",
    "StrategyRiskManager",
    "TelegramNotifier",
    "initialise",
    "shutdown",
    "get_container",
    "require_container",
    "set_container",
]

