"""Application-wide service container initialised during FastAPI startup."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

from loguru import logger

from src.cache import init_redis
from src.config import load_project_env
from src.ibbot.config import ConfigProvider
from src.db import DatabaseConfigError, MariaDBConfigurationError, get_database_url
from src.utils import now_local
from src.websocket_manager import WebSocketManager, websocket_manager


@dataclass(slots=True)
class CoreContainer:
    """Shared state required by the UI application."""

    redis_client: Any | None
    database_url: str | None
    config_provider: ConfigProvider | None
    websocket_manager: WebSocketManager


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


def _redact_database_url(url: str | None) -> str | None:
    if not url:
        return None

    try:
        parsed = urlparse(url)
    except Exception:  # pragma: no cover - defensive parsing guard
        return url

    netloc = parsed.netloc
    if "@" in netloc:
        userinfo, host = netloc.rsplit("@", 1)
        if ":" in userinfo:
            username = userinfo.split(":", 1)[0]
            userinfo = f"{username}:***"
        elif userinfo:
            userinfo = f"{userinfo}:***"
        netloc = f"{userinfo}@{host}" if userinfo else host

    return urlunparse(parsed._replace(netloc=netloc))


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

    database_url: str | None = None
    try:
        database_url = get_database_url()
    except (DatabaseConfigError, MariaDBConfigurationError) as exc:
        logger.warning("Database configuration unavailable: {}", exc)
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Unexpected error while resolving database URL: {}", exc)
    else:
        redacted_url = _redact_database_url(database_url)
        if redacted_url:
            logger.info("Resolved database configuration: {}", redacted_url)

    config_provider: ConfigProvider | None = None
    try:
        config_provider = ConfigProvider(redis_client=redis_client)
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("ConfigProvider initialisation failed: {}", exc)
        config_provider = None

    config_provider = await _start_config_provider(config_provider)

    container = CoreContainer(
        redis_client=redis_client,
        database_url=database_url,
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

    # Close Redis connection first to ensure proper cleanup
    if container.redis_client is not None:
        try:
            from src.cache import close_redis
            await close_redis()
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.warning("Error while closing Redis client: {}", exc)

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
