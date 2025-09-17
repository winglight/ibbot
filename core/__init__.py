from src.connection_manager import connection_manager, ConnectionConfig
from src.ibkr_client import IBKRClient
from src.telegram_notifier import TelegramNotifier
from src.risk_manager import RiskManager
from src.strategy_manager import StrategyManager
from src.database import get_db_manager
from src.queue_manager import queue_manager
from src.order_manager import get_order_manager
from src.websocket_manager import websocket_manager
from dataclasses import dataclass, field
from typing import List
from loguru import logger

from src.depth_subscriber import DepthSubscriber
from src.notification_manager import NotificationManager
from src.dom_processor import DOMProcessor
from src.dom_service import get_dom_service
from src.strategies.dom_strategy import DOMStrategy

# Initialize shared instances
telegram_notifier = TelegramNotifier()
notification_manager = NotificationManager(
    telegram_notifier=telegram_notifier,
    websocket_manager=websocket_manager,
    db_manager=get_db_manager(),
)

@dataclass
class DOMConfig:
    window: int = 10000
    max_levels: int = 30
    trend_thresholds: List[float] = field(
        default_factory=lambda: [0.5, 0.7, 0.85, 0.95]
    )


dom_config = DOMConfig()
dom_processor = DOMProcessor(window=dom_config.window, max_levels=dom_config.max_levels)

depth_subscriber = DepthSubscriber(connection_manager)

risk_manager = RiskManager(notification_manager=notification_manager)
strategy_manager = StrategyManager()

try:
    strategy_manager.set_dependencies(risk_manager=risk_manager, queue_manager=queue_manager)
except Exception:
    pass

order_manager = get_order_manager()
try:
    order_manager.set_dependencies(
        websocket_manager=websocket_manager,
        notification_manager=notification_manager,
        risk_manager=risk_manager,
    )
except Exception:
    pass

try:
    risk_manager.set_dependencies(order_manager=order_manager)
except Exception:
    pass

try:
    queue_manager.set_dependencies(
        risk_manager=risk_manager,
        strategy_manager=strategy_manager,
        websocket_manager=websocket_manager,
        order_manager=order_manager,
    )
except Exception:
    pass

# Register the DOM internal strategy so it loads automatically
dom_strategy = None
try:
    dom_strategy = DOMStrategy(dom_service=get_dom_service())
    dom_strategy.set_dependencies(
        strategy_manager=strategy_manager,
        order_manager=order_manager,
        queue_manager=queue_manager,
        risk_manager=risk_manager,
        notification_manager=notification_manager,
    )
    strategy_manager.register_internal_strategy(dom_strategy)
except Exception as exc:  # pragma: no cover - defensive guard
    logger.warning(f"Failed to initialize DOM strategy: {exc}")

# IBKR client is created on demand
ibkr_client = None
