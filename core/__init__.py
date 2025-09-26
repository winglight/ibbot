from src.connection_manager import connection_manager, ConnectionConfig
from src.ibkr_client import IBKRClient
from src.telegram_notifier import TelegramNotifier
from src.risk_manager import RiskManager
from src.strategy_manager import StrategyManager
from src.database import get_db_manager
from src.queue_manager import queue_manager
from src.order_manager import get_order_manager
from src.websocket_manager import websocket_manager
from loguru import logger

from src.notification_manager import NotificationManager
from src.dom_processor import DOMProcessor
from src.dom_service import get_dom_service
from src.strategies.dom_strategy import DOMStrategy
from src.dom_dependencies import DOMConfig, get_dom_processor
import src.dom_dependencies as dom_dependencies

# Initialize shared instances
telegram_notifier = TelegramNotifier()
notification_manager = NotificationManager(
    telegram_notifier=telegram_notifier,
    websocket_manager=websocket_manager,
    db_manager=get_db_manager(),
)

dom_config = dom_dependencies.dom_config
dom_processor = get_dom_processor()
depth_subscriber = dom_dependencies.depth_subscriber

risk_manager = RiskManager(notification_manager=notification_manager)
strategy_manager = StrategyManager()

try:
    strategy_manager.set_dependencies(
        risk_manager=risk_manager,
        queue_manager=queue_manager,
        notification_manager=notification_manager,
    )
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
        notification_manager=notification_manager,
    )
except Exception:
    pass

# Register the DOM internal strategy so it loads automatically
dom_strategy = None
try:
    dom_strategy = DOMStrategy()
    dom_strategy.set_dependencies(
        dom_service=get_dom_service(),
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
