"""Base classes for internal trading strategies."""

from __future__ import annotations

from typing import Optional, Any

from loguru import logger


class BaseStrategy:
    """Common scaffolding for internal strategies."""

    def __init__(self, name: str, description: str = "") -> None:
        self.name = name
        self.description = description or name
        self.enabled: bool = True
        self.active: bool = False
        self.strategy_manager: Optional[Any] = None
        self.order_manager: Optional[Any] = None
        self.queue_manager: Optional[Any] = None
        self.risk_manager: Optional[Any] = None
        self.notification_manager: Optional[Any] = None
        self.logger = logger.bind(strategy=name)

    # ------------------------------------------------------------------
    def set_dependencies(
        self,
        *,
        strategy_manager: Optional[Any] = None,
        order_manager: Optional[Any] = None,
        queue_manager: Optional[Any] = None,
        risk_manager: Optional[Any] = None,
        notification_manager: Optional[Any] = None,
    ) -> None:
        """Inject runtime dependencies."""

        if strategy_manager is not None:
            self.strategy_manager = strategy_manager
        if order_manager is not None:
            self.order_manager = order_manager
        if queue_manager is not None:
            self.queue_manager = queue_manager
        if risk_manager is not None:
            self.risk_manager = risk_manager
        if notification_manager is not None:
            self.notification_manager = notification_manager

    # ------------------------------------------------------------------
    def start(self) -> bool:
        """Activate the strategy runtime."""
        if self.active:
            return False
        self.active = True
        self.logger.info("Strategy started")
        return True

    # ------------------------------------------------------------------
    def stop(self) -> bool:
        """Deactivate the strategy runtime."""
        if not self.active:
            return False
        self.active = False
        self.logger.info("Strategy stopped")
        return True

    # ------------------------------------------------------------------
    def enable(self) -> None:
        """Enable and start the strategy."""
        self.enabled = True
        self.start()

    # ------------------------------------------------------------------
    def disable(self) -> None:
        """Disable and stop the strategy."""
        self.enabled = False
        self.stop()

    # ------------------------------------------------------------------
    def on_registered(self) -> None:
        """Hook called when the strategy is registered with the manager."""
        return None

