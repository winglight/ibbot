import asyncio
import atexit
import os

from src.connection_manager import connection_manager, ConnectionConfig
from src.ibkr_client import IBKRClient
from src.telegram_notifier import TelegramNotifier
from src.risk_manager import RiskManager
from src.strategy_manager import StrategyManager
from src.database import get_db_manager
from src.async_executor import get_async_executor
from src.queue_manager import queue_manager
from src.order_manager import get_order_manager
from src.websocket_manager import websocket_manager
from src.market_data_stream import PriceVolumeStream
from src.atr_trailing_service import ATRTrailingService
from loguru import logger

from src.notification_manager import NotificationManager
from src.account_snapshot_service import AccountSnapshotService
from src.dom_processor import DOMProcessor
from src.dom_service import get_dom_service
from src.strategies.dom_strategy import DOMStrategy
from src.strategies.buy_the_dip_strategy import BuyTheDipStrategy
from src.dom_dependencies import DOMConfig, get_dom_processor
import src.dom_dependencies as dom_dependencies
from src.strategy_risk import StrategyRiskManager
from src.cache import init_redis
from src.utils import (
    configure_async_executor,
    run_in_cpu_executor,
)

# Initialize shared instances
_cpu_default = min(32, os.cpu_count() or 4)


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        logger.warning(f"Invalid integer for {name!s}: {value!r}; using default {default}")
        return default
    return parsed if parsed > 0 else default


async_executor = configure_async_executor(
    db_max_workers=_env_int("DB_EXECUTOR_WORKERS", 4),
    cpu_max_workers=_env_int("CPU_EXECUTOR_WORKERS", _cpu_default),
)
atexit.register(async_executor.shutdown)

telegram_notifier = TelegramNotifier()
async_executor = get_async_executor()
db_manager = get_db_manager(async_executor=async_executor)
notification_manager = NotificationManager(
    telegram_notifier=telegram_notifier,
    websocket_manager=websocket_manager,
    db_manager=db_manager,
)

dom_config = dom_dependencies.dom_config
dom_processor = get_dom_processor()
depth_subscriber = dom_dependencies.depth_subscriber

risk_manager = RiskManager(notification_manager=notification_manager, async_executor=async_executor)
strategy_manager = StrategyManager(async_executor=async_executor)
strategy_risk_manager = StrategyRiskManager(
    db_manager=db_manager,
    notification_manager=notification_manager,
)
price_volume_stream = PriceVolumeStream()
atr_trailing_service = ATRTrailingService(
    price_stream=price_volume_stream,
    risk_manager=risk_manager,
    notification_manager=notification_manager,
)

redis_client = None


def _initialise_redis_in_background() -> None:
    """Initialise Redis without blocking the main import path."""

    global redis_client
    try:
        client = asyncio.run(init_redis())
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning(f"Redis initialisation skipped: {exc}")
        return

    if client is not None:
        redis_client = client
        logger.info("Redis client ready (background thread)")
    else:
        redis_client = None
        logger.info("Redis client unavailable after background initialisation")


def _rebuild_strategy_cache_in_background() -> None:
    """Rebuild the strategy cache without blocking FastAPI startup."""

    try:
        strategy_manager.rebuild_strategy_cache(block=True)
        logger.info("Strategy cache rebuilt in background thread")
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning(f"Failed to rebuild strategy cache during bootstrap: {exc}")


def _start_background_threads() -> None:
    logger.info("Scheduling background initialisation tasks (Redis, strategy cache)")

    run_in_cpu_executor(_initialise_redis_in_background)
    run_in_cpu_executor(_rebuild_strategy_cache_in_background)

try:
    strategy_manager.set_dependencies(
        risk_manager=risk_manager,
        queue_manager=queue_manager,
        notification_manager=notification_manager,
        strategy_risk_manager=strategy_risk_manager,
    )
except Exception:
    pass

_start_background_threads()

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
    risk_manager.set_dependencies(order_manager=order_manager, atr_service=atr_trailing_service)
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

try:
    strategy_risk_manager.set_dependencies(
        strategy_manager=strategy_manager,
        queue_manager=queue_manager,
        notification_manager=notification_manager,
    )
    strategy_risk_manager.start()
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
        strategy_risk_manager=strategy_risk_manager,
        notification_manager=notification_manager,
    )
    strategy_manager.register_internal_strategy(dom_strategy)
except Exception as exc:  # pragma: no cover - defensive guard
    logger.warning(f"Failed to initialize DOM strategy: {exc}")

buy_the_dip_strategy = None
try:
    buy_the_dip_strategy = BuyTheDipStrategy()
    buy_the_dip_strategy.set_dependencies(
        strategy_manager=strategy_manager,
        order_manager=order_manager,
        queue_manager=queue_manager,
        risk_manager=risk_manager,
        strategy_risk_manager=strategy_risk_manager,
        notification_manager=notification_manager,
    )
    strategy_manager.register_internal_strategy(buy_the_dip_strategy)
except Exception as exc:  # pragma: no cover - defensive guard
    logger.warning(f"Failed to initialize Buy-the-Dip strategy: {exc}")

# IBKR client is created on demand
ibkr_client = None

account_snapshot_service = AccountSnapshotService()
