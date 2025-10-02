"""Core application wiring for the UI service.

The UI runtime now relies on :class:`~core.container.CoreContainer` to lazily
initialise shared dependencies (Redis, configuration providers, DOM helpers,
etc.) instead of importing globals at module import time.  ``get_core_container``
returns the singleton container used by the FastAPI application.
"""

from __future__ import annotations

from typing import Any, Optional

from src.ibkr_client import IBKRClient

from .container import CoreContainer

__all__ = [
    "CoreContainer",
    "IBKRClient",
    "get_core_container",
    "set_core_container",
    "initialise_redis",
    "initialise_redis_sync",
    "get_config_provider",
    "get_dom_service",
    "get_db_manager",
    "get_data_source_manager",
    "get_notification_manager",
    "get_order_manager",
    "get_queue_manager",
    "get_risk_manager",
    "get_strategy_manager",
    "get_strategy_risk_manager",
    "get_telegram_notifier",
    "ibkr_client",
    "get_ibkr_client",
    "set_ibkr_client",
]

_container: Optional[CoreContainer] = None
ibkr_client: Optional[IBKRClient] = None


def get_core_container() -> CoreContainer:
    """Return the lazily-initialised core dependency container."""

    global _container
    if _container is None:
        _container = CoreContainer()
    return _container


def set_core_container(container: Optional[CoreContainer]) -> None:
    """Override the global container (used primarily by tests)."""

    global _container
    _container = container


def set_ibkr_client(client: Optional[IBKRClient]) -> None:
    """Update the shared IBKR client reference used by the UI."""

    global ibkr_client
    ibkr_client = client


def get_ibkr_client() -> Optional[IBKRClient]:
    """Return the cached IBKR client instance, if available."""

    return ibkr_client


async def initialise_redis(*, force: bool = False) -> Optional[Any]:
    """Asynchronously initialise the Redis client via the container."""

    container = get_core_container()
    return await container.ensure_redis(force=force)


def initialise_redis_sync(*, force: bool = False) -> Optional[Any]:
    """Synchronously initialise the Redis client via the container."""

    container = get_core_container()
    return container.ensure_redis_sync(force=force)


async def get_config_provider():
    """Return the lazily cached configuration provider."""

    container = get_core_container()
    return await container.get_config_provider()


def get_dom_service():
    """Expose the shared DOM service instance via the container."""

    container = get_core_container()
    return container.dom_service


def get_db_manager():
    """Expose the shared database manager via the container."""

    container = get_core_container()
    return container.get_db_manager()


def get_data_source_manager():
    """Expose the unified data source manager (Redis/Influx pipeline)."""

    container = get_core_container()
    return container.get_data_source_manager()


def get_notification_manager():
    """Return the lazily-instantiated notification manager."""

    container = get_core_container()
    return container.get_notification_manager()


def get_order_manager():
    """Return the shared order manager instance."""

    container = get_core_container()
    return container.get_order_manager()


def get_queue_manager():
    """Return the shared queue manager instance."""

    container = get_core_container()
    return container.get_queue_manager()


def get_risk_manager():
    """Return the shared risk manager instance."""

    container = get_core_container()
    return container.get_risk_manager()


def get_strategy_manager():
    """Return the shared strategy manager instance."""

    container = get_core_container()
    return container.get_strategy_manager()


def get_strategy_risk_manager():
    """Return the shared per-strategy risk manager."""

    container = get_core_container()
    return container.get_strategy_risk_manager()


def get_telegram_notifier():
    """Return the shared Telegram notifier."""

    container = get_core_container()
    return container.telegram_notifier


def __getattr__(name: str) -> Any:
    """Delegate legacy attribute lookups to the dependency container."""

    container = get_core_container()
    if name == "connection_manager":
        return container.connection_manager
    if name == "websocket_manager":
        return container.websocket_manager
    if name == "dom_config":
        return container.dom_config
    if name == "dom_processor":
        return container.dom_processor
    if name == "depth_subscriber":
        return container.depth_subscriber
    if name == "redis_client":
        return container.redis_client
    if name == "notification_manager":
        return container.get_notification_manager()
    if name == "order_manager":
        return container.get_order_manager()
    if name == "queue_manager":
        return container.get_queue_manager()
    if name == "risk_manager":
        return container.get_risk_manager()
    if name == "strategy_manager":
        return container.get_strategy_manager()
    if name == "strategy_risk_manager":
        return container.get_strategy_risk_manager()
    if name == "telegram_notifier":
        return container.telegram_notifier
    raise AttributeError(f"module 'core' has no attribute '{name}'")
