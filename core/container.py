"""Dependency container for the UI runtime.

The FastAPI application consumes a subset of the broader backend services.
``CoreContainer`` provides a thin wrapper that lazily wires the components the
UI requires (Redis, configuration provider, WebSocket manager, DOM helpers,
etc.) while keeping the legacy modules decoupled from global imports.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from loguru import logger

from src.cache import init_redis
from src.connection_manager import ConnectionManager, connection_manager
from src.configuration.provider import ConfigProvider, get_provider
from src.database import DatabaseManager, get_db_manager
from src.data_layer.bootstrap import get_data_source_manager as resolve_data_source_manager
from src.data_layer.data_source_manager import DataSourceManager
from src.dom_dependencies import (
    DOMConfig,
    depth_subscriber as default_depth_subscriber,
    dom_config as default_dom_config,
    get_dom_processor,
)
from src.dom_processor import DOMProcessor
from src.dom_service import DOMService, get_dom_service
from src.websocket_manager import WebSocketManager, websocket_manager

RedisFactory = Callable[[bool], Awaitable[Optional[Any]]]
ConfigProviderFactory = Callable[[], Awaitable[ConfigProvider]]
ConnectionManagerFactory = Callable[[], ConnectionManager]
WebSocketManagerFactory = Callable[[], WebSocketManager]
DomServiceFactory = Callable[[], DOMService]
DomConfigFactory = Callable[[], DOMConfig]
DomProcessorFactory = Callable[[], DOMProcessor]
DepthSubscriberFactory = Callable[[], Any]
DatabaseManagerFactory = Callable[[], DatabaseManager]
DataSourceManagerFactory = Callable[[], DataSourceManager]


@dataclass
class CoreContainer:
    """Simple dependency injection container for UI runtime services."""

    redis_initializer: RedisFactory = init_redis
    config_provider_factory: ConfigProviderFactory = get_provider
    connection_manager_factory: ConnectionManagerFactory = lambda: connection_manager
    websocket_manager_factory: WebSocketManagerFactory = lambda: websocket_manager
    dom_service_factory: DomServiceFactory = get_dom_service
    dom_config_factory: DomConfigFactory = lambda: default_dom_config
    dom_processor_factory: DomProcessorFactory = get_dom_processor
    depth_subscriber_factory: DepthSubscriberFactory = lambda: default_depth_subscriber
    db_manager_factory: DatabaseManagerFactory = get_db_manager
    data_source_manager_factory: DataSourceManagerFactory = resolve_data_source_manager

    _redis_client: Optional[Any] = field(default=None, init=False)
    _config_provider: Optional[ConfigProvider] = field(default=None, init=False)
    _connection_manager: Optional[ConnectionManager] = field(default=None, init=False)
    _websocket_manager: Optional[WebSocketManager] = field(default=None, init=False)
    _dom_service: Optional[DOMService] = field(default=None, init=False)
    _dom_config: Optional[DOMConfig] = field(default=None, init=False)
    _dom_processor: Optional[DOMProcessor] = field(default=None, init=False)
    _depth_subscriber: Optional[Any] = field(default=None, init=False)
    _db_manager: Optional[DatabaseManager] = field(default=None, init=False)
    _data_source_manager: Optional[DataSourceManager] = field(default=None, init=False)

    @property
    def connection_manager(self) -> ConnectionManager:
        if self._connection_manager is None:
            self._connection_manager = self.connection_manager_factory()
        return self._connection_manager

    @property
    def websocket_manager(self) -> WebSocketManager:
        if self._websocket_manager is None:
            self._websocket_manager = self.websocket_manager_factory()
        return self._websocket_manager

    @property
    def dom_service(self) -> DOMService:
        if self._dom_service is None:
            self._dom_service = self.dom_service_factory()
        return self._dom_service

    @property
    def dom_config(self) -> DOMConfig:
        if self._dom_config is None:
            self._dom_config = self.dom_config_factory()
        return self._dom_config

    @property
    def dom_processor(self) -> DOMProcessor:
        if self._dom_processor is None:
            self._dom_processor = self.dom_processor_factory()
        return self._dom_processor

    @property
    def depth_subscriber(self) -> Any:
        if self._depth_subscriber is None:
            self._depth_subscriber = self.depth_subscriber_factory()
        return self._depth_subscriber

    @property
    def redis_client(self) -> Optional[Any]:
        return self._redis_client

    def get_db_manager(self) -> DatabaseManager:
        if self._db_manager is None:
            self._db_manager = self.db_manager_factory()
        return self._db_manager

    def get_data_source_manager(self) -> DataSourceManager:
        if self._data_source_manager is None:
            self._data_source_manager = self.data_source_manager_factory()
        return self._data_source_manager

    async def ensure_redis(self, *, force: bool = False) -> Optional[Any]:
        """Initialise the Redis client lazily and cache it on success."""

        if self._redis_client is not None and not force:
            return self._redis_client

        try:
            client = await self.redis_initializer(force=force)
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.warning("Redis initialisation skipped", error=str(exc))
            client = None

        if client is not None or force:
            self._redis_client = client
        return client

    def ensure_redis_sync(self, *, force: bool = False) -> Optional[Any]:
        """Synchronous helper for environments without an event loop."""

        try:
            return asyncio.run(self.ensure_redis(force=force))
        except RuntimeError as exc:  # pragma: no cover - defensive guard
            logger.warning("Redis initialisation aborted", error=str(exc))
            return None

    async def get_config_provider(self) -> ConfigProvider:
        if self._config_provider is None:
            self._config_provider = await self.config_provider_factory()
        return self._config_provider


__all__ = ["CoreContainer"]
