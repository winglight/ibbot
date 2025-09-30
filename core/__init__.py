"""Runtime container helpers for application-wide services."""

from __future__ import annotations

from typing import Any

from .container import CoreContainer, get_container, initialise, require_container, set_container, shutdown
from src.connection_manager import connection_manager
from src.database import get_db_manager
from src.dom_service import get_dom_service
from src.ibbot.influx import configure_influx
from src.ibkr_client import IBKRClient
from src.notification_manager import NotificationManager
from src.order_manager import get_order_manager
from src.queue_manager import queue_manager
from src.risk_manager import RiskManager
from src.strategy_manager import StrategyManager
from src.strategy_risk import StrategyRiskManager
from src.telegram_notifier import TelegramNotifier
from src.websocket_manager import websocket_manager

__all__ = [
    "CoreContainer",
    "IBKRClient",
    "NotificationManager",
    "RiskManager",
    "StrategyManager",
    "StrategyRiskManager",
    "TelegramNotifier",
    "configure_influx",
    "connection_manager",
    "get_container",
    "get_db_manager",
    "get_dom_service",
    "get_order_manager",
    "initialise",
    "queue_manager",
    "require_container",
    "set_container",
    "shutdown",
    "websocket_manager",
]


class _LazyProxy:
    def __init__(self, attr: str) -> None:
        self._attr = attr

    def _resolve(self) -> Any:
        return getattr(require_container(), self._attr)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._resolve(), item)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        target = self._resolve()
        return target(*args, **kwargs)

    def __bool__(self) -> bool:
        try:
            return bool(self._resolve())
        except RuntimeError:
            return False

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        container = get_container()
        if container is None:
            return f"<core.LazyProxy pending '{self._attr}'>"
        try:
            return repr(getattr(container, self._attr))
        except AttributeError:
            return f"<core.LazyProxy missing '{self._attr}'>"


def __getattr__(name: str) -> Any:  # pragma: no cover - passthrough helper
    container = get_container()
    if container is None:
        return _LazyProxy(name)
    try:
        return getattr(container, name)
    except AttributeError as exc:
        raise AttributeError(name) from exc

