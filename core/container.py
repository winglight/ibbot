"""Application-wide service container initialised during FastAPI startup."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger

from src.cache import init_redis
from src.config import load_project_env
from src.ibbot.config import ConfigProvider
from src.ibbot.influx import InfluxConfig, configure_influx
from src.utils import now_local
from src.websocket_manager import WebSocketManager, websocket_manager


@dataclass(slots=True)
class CoreContainer:
    """Shared state required by the UI application."""

    redis_client: Any | None
    influx_config: InfluxConfig | None
    config_provider: ConfigProvider | None
    websocket_manager: WebSocketManager
    telegram_notifier: Any | None = None
    notification_manager: Any | None = None
    risk_manager: Any | None = None
    strategy_risk_manager: Any | None = None
    strategy_manager: Any | None = None
    queue_manager: Any | None = None


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


async def _start_config_provider(provider: ConfigProvider | None) -> ConfigProvider | None:
    if provider is None:
        return None
    try:
        await provider.start()
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Failed to start ConfigProvider listener: {}", exc)
        return None
    return provider


async def initialise(*, force: bool = False) -> CoreContainer:
    """Initialise shared services and return the application container."""

    if _container is not None and not force:
        return _container

    load_project_env()
    logger.info("Initialising application container at {}", now_local())

    try:
        redis_client = await init_redis()
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Redis initialisation failed: {}", exc)
        redis_client = None

    influx_config: InfluxConfig | None = None
    try:
        influx_config = configure_influx()
    except Exception as exc:  # pragma: no cover - optional dependency errors
        logger.warning("Influx configuration failed: {}", exc)

    config_provider: ConfigProvider | None = None
    try:
        config_provider = ConfigProvider(redis_client=redis_client)
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("ConfigProvider initialisation failed: {}", exc)
        config_provider = None

    config_provider = await _start_config_provider(config_provider)

    container = CoreContainer(
        redis_client=redis_client,
        influx_config=influx_config,
        config_provider=config_provider,
        websocket_manager=websocket_manager,
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
    "ConfigProvider",
    "CoreContainer",
    "initialise",
    "shutdown",
    "get_container",
    "require_container",
    "set_container",
]
