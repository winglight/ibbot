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
from src.notification_manager import NotificationManager
from src.order_manager import OrderManager, get_order_manager as resolve_order_manager
from src.queue_manager import QueueManager, queue_manager as legacy_queue_manager
from src.risk_manager import RiskManager
from src.strategy_manager import StrategyManager
from src.strategy_risk import StrategyRiskManager
from src.telegram_notifier import TelegramNotifier

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
NotificationManagerFactory = Callable[[], NotificationManager]
OrderManagerFactory = Callable[[], OrderManager]
QueueManagerFactory = Callable[[], QueueManager]
RiskManagerFactory = Callable[[NotificationManager], RiskManager]
StrategyManagerFactory = Callable[[], StrategyManager]
StrategyRiskManagerFactory = Callable[[DatabaseManager, NotificationManager], StrategyRiskManager]
TelegramNotifierFactory = Callable[[], TelegramNotifier]


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
    notification_manager_factory: NotificationManagerFactory = NotificationManager
    order_manager_factory: OrderManagerFactory = resolve_order_manager
    queue_manager_factory: QueueManagerFactory = lambda: legacy_queue_manager
    risk_manager_factory: RiskManagerFactory = lambda manager: RiskManager(manager)
    strategy_manager_factory: StrategyManagerFactory = StrategyManager
    strategy_risk_manager_factory: StrategyRiskManagerFactory = (
        lambda db_manager, notification_manager: StrategyRiskManager(
            db_manager=db_manager, notification_manager=notification_manager
        )
    )
    telegram_notifier_factory: TelegramNotifierFactory = TelegramNotifier

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
    _notification_manager: Optional[NotificationManager] = field(default=None, init=False)
    _order_manager: Optional[OrderManager] = field(default=None, init=False)
    _queue_manager: Optional[QueueManager] = field(default=None, init=False)
    _risk_manager: Optional[RiskManager] = field(default=None, init=False)
    _strategy_manager: Optional[StrategyManager] = field(default=None, init=False)
    _strategy_risk_manager: Optional[StrategyRiskManager] = field(default=None, init=False)
    _telegram_notifier: Optional[TelegramNotifier] = field(default=None, init=False)
    _ensuring_strategy_runtime: bool = field(default=False, init=False)

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

    @property
    def telegram_notifier(self) -> TelegramNotifier:
        if self._telegram_notifier is None:
            self._telegram_notifier = self.telegram_notifier_factory()
        return self._telegram_notifier

    def get_notification_manager(self) -> NotificationManager:
        if self._notification_manager is None:
            manager = self.notification_manager_factory()
            manager.set_dependencies(
                telegram_notifier=self.telegram_notifier,
                websocket_manager=self.websocket_manager,
                db_manager=self.get_db_manager(),
            )
            self._notification_manager = manager
        else:
            self._notification_manager.set_dependencies(
                telegram_notifier=self.telegram_notifier,
                websocket_manager=self.websocket_manager,
                db_manager=self.get_db_manager(),
            )
        return self._notification_manager

    def _ensure_strategy_runtime(self) -> None:
        if self._strategy_manager is not None or self._ensuring_strategy_runtime:
            return

        self._ensuring_strategy_runtime = True
        try:
            notification_manager = self.get_notification_manager()
            db_manager = self.get_db_manager()
            websocket_manager = self.websocket_manager

            order_manager = self._order_manager
            if order_manager is None:
                order_manager = self.order_manager_factory()
                self._order_manager = order_manager

            risk_manager = self._risk_manager
            if risk_manager is None:
                risk_manager = self.risk_manager_factory(notification_manager)
                self._risk_manager = risk_manager

            queue_manager = self._queue_manager
            if queue_manager is None:
                queue_manager = self.queue_manager_factory()
                self._queue_manager = queue_manager

            strategy_manager = self._strategy_manager
            if strategy_manager is None:
                strategy_manager = self.strategy_manager_factory()
                self._strategy_manager = strategy_manager

            strategy_risk_manager = self._strategy_risk_manager
            if strategy_risk_manager is None:
                strategy_risk_manager = self.strategy_risk_manager_factory(
                    db_manager, notification_manager
                )
                self._strategy_risk_manager = strategy_risk_manager

            notification_manager.set_dependencies(
                telegram_notifier=self.telegram_notifier,
                websocket_manager=websocket_manager,
                db_manager=db_manager,
            )
            order_manager.set_dependencies(
                websocket_manager=websocket_manager,
                notification_manager=notification_manager,
                risk_manager=risk_manager,
            )
            risk_manager.set_dependencies(order_manager=order_manager)
            queue_manager.set_dependencies(
                risk_manager=risk_manager,
                strategy_manager=strategy_manager,
                websocket_manager=websocket_manager,
                order_manager=order_manager,
                notification_manager=notification_manager,
            )
            strategy_manager.set_dependencies(
                risk_manager=risk_manager,
                queue_manager=queue_manager,
                notification_manager=notification_manager,
                strategy_risk_manager=strategy_risk_manager,
            )
            strategy_risk_manager.set_dependencies(
                strategy_manager=strategy_manager,
                queue_manager=queue_manager,
                notification_manager=notification_manager,
            )
        finally:
            self._ensuring_strategy_runtime = False

    def get_order_manager(self) -> OrderManager:
        self._ensure_strategy_runtime()
        if self._order_manager is None:
            raise RuntimeError("Order manager failed to initialise")
        return self._order_manager

    def get_queue_manager(self) -> QueueManager:
        self._ensure_strategy_runtime()
        if self._queue_manager is None:
            raise RuntimeError("Queue manager failed to initialise")
        return self._queue_manager

    def get_risk_manager(self) -> RiskManager:
        self._ensure_strategy_runtime()
        if self._risk_manager is None:
            raise RuntimeError("Risk manager failed to initialise")
        return self._risk_manager

    def get_strategy_manager(self) -> StrategyManager:
        self._ensure_strategy_runtime()
        if self._strategy_manager is None:
            raise RuntimeError("Strategy manager failed to initialise")
        return self._strategy_manager

    def get_strategy_risk_manager(self) -> StrategyRiskManager:
        self._ensure_strategy_runtime()
        if self._strategy_risk_manager is None:
            raise RuntimeError("Strategy risk manager failed to initialise")
        return self._strategy_risk_manager

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
