from src.connection_manager import connection_manager, ConnectionConfig
from src.ibkr_client import IBKRClient
from src.telegram_notifier import TelegramNotifier
from src.risk_manager import RiskManager
from src.strategy_manager import StrategyManager
from src.database import get_db_manager
from src.queue_manager import queue_manager
from src.order_manager import get_order_manager
from src.websocket_manager import websocket_manager
from dataclasses import dataclass

from src.depth_subscriber import DepthSubscriber
from src.notification_manager import NotificationManager
from src.dom_processor import DOMProcessor

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
        connection_manager=connection_manager,
        websocket_manager=websocket_manager,
        order_manager=order_manager,
    )
except Exception:
    pass

# IBKR client is created on demand
ibkr_client = None
