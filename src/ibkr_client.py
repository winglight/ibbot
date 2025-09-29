import asyncio
import itertools
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

try:  # pragma: no cover - fallback used when ib_insync is unavailable
    from ib_insync import Contract, Future, IB, LimitOrder, MarketOrder, Order, Stock
except ImportError:  # pragma: no cover - exercised only in minimal environments
    from test.ib_insync_stub import Contract, Future, IB, LimitOrder, MarketOrder, Order, Stock
from loguru import logger
from zoneinfo import ZoneInfo

from src.utils import is_trading_disabled, now_local, to_local_time
from src.data_layer import (
    DataSubscriptionRequest,
    DataSubscriptionToken,
    DataSourceError,
    get_data_layer_mode,
    get_data_source_manager,
    is_dual_mode,
    is_legacy_mode,
)

# Module-specific logger for ibkr_client
ibkr_logger = logger.bind(module="ibkr_client", request_id="-")
import time
import threading
import os
import csv
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
import math
import subprocess
from .connection_manager import connection_manager, connection_logger
from .database import get_db_manager
from .restart_utils import restart_program

NY_TZ = ZoneInfo("America/New_York")


INDEX_FUTURE_DETAILS: Dict[str, Dict[str, str]] = {
    "ES": {"exchange": "CME", "multiplier": "50", "tradingClass": "ES"},
    "MES": {"exchange": "CME", "multiplier": "5", "tradingClass": "MES"},
    "NQ": {"exchange": "CME", "multiplier": "20", "tradingClass": "NQ"},
    "MNQ": {"exchange": "CME", "multiplier": "2", "tradingClass": "MNQ"},
    "YM": {"exchange": "ECBOT", "multiplier": "5", "tradingClass": "YM"},
    "MYM": {"exchange": "ECBOT", "multiplier": "0.5", "tradingClass": "MYM"},
    "RTY": {"exchange": "CME", "multiplier": "50", "tradingClass": "RTY"},
    "M2K": {"exchange": "CME", "multiplier": "5", "tradingClass": "M2K"},
    "VX": {"exchange": "CFE", "multiplier": "1000", "tradingClass": "VX"},
}

INDEX_FUTURE_SYMBOLS = frozenset(INDEX_FUTURE_DETAILS.keys())
_QUARTERLY_CONTRACT_MONTHS = (3, 6, 9, 12)


def _third_friday_datetime(year: int, month: int) -> datetime:
    """Return the datetime (NY timezone) of the contract's third Friday open."""

    first_day = datetime(year, month, 1, tzinfo=NY_TZ)
    # ``weekday`` -> Monday=0 ... Sunday=6; Friday is 4
    days_until_friday = (4 - first_day.weekday()) % 7
    third_friday_day = 1 + days_until_friday + 14
    # CME 股指期货在合约到期当日美东早盘停止交易
    return datetime(year, month, third_friday_day, 9, 30, tzinfo=NY_TZ)


def _front_quarter_contract(now: datetime) -> Tuple[int, int]:
    """Determine the nearest active quarterly contract (year, month)."""

    ny_now = now.astimezone(NY_TZ)
    year = ny_now.year
    month = ny_now.month

    # Evaluate the remaining quarterly contracts for the current year first,
    # then fall back to the next year's cycle.  This avoids skipping the
    # current-year contracts when ``month`` is greater than the candidate
    # (e.g. October should still select December, not next March).
    for year_offset in (0, 1):
        candidate_year = year + year_offset
        for candidate_month in _QUARTERLY_CONTRACT_MONTHS:
            if year_offset == 0 and candidate_month < month:
                continue

            expiry = _third_friday_datetime(candidate_year, candidate_month)
            if ny_now < expiry:
                return candidate_year, candidate_month

    # Fallback – should not be hit because the loop above covers current and
    # next year, but keep a defensive default.
    return year + 1, _QUARTERLY_CONTRACT_MONTHS[0]

class IBKRClient:
    @staticmethod
    def _parse_env_float(key: str, default: float) -> float:
        raw = os.getenv(key)
        if raw is None:
            return float(default)
        try:
            cleaned = raw.split('#', 1)[0].strip()
            if not cleaned:
                return float(default)
            return max(0.0, float(cleaned))
        except Exception:
            try:
                ibkr_logger.warning(f"Invalid float for {key}='{raw}', falling back to {default}")
            except Exception:
                pass
            return float(default)

    @staticmethod
    def _parse_env_bool(key: str, default: bool) -> bool:
        raw = os.getenv(key)
        if raw is None:
            return bool(default)

        try:
            cleaned = raw.split('#', 1)[0].strip().lower()
        except Exception:
            return bool(default)

        if cleaned == "":
            return bool(default)

        return cleaned in {"1", "true", "yes", "y", "on"}

    def __init__(self):
        self.connection_manager = connection_manager

        # 设置ConnectionManager对IBKRClient的弱引用，用于错误处理
        import weakref
        self.connection_manager._ibkr_client_ref = weakref.ref(self)

        self._process_restart_enabled = self._parse_env_bool(
            "IBKR_ENABLE_PROCESS_RESTART",
            False,
        )
        self._self_restart_suppressed_logged = False

        self.order_backup_file = "data/orders_backup.csv"
        self._ensure_backup_directory()
        # Price cache to track real-time prices
        self.price_cache = {}
        self.price_cache_timestamp = {}
        # Cache TTL seconds (used to decide when to refresh real-time data)
        self.price_cache_ttl_seconds = 5  # 减少到5秒，确保实时按钮能获取最新价格
        # 映射reqId到订单ID，用于错误处理
        self.req_id_to_order_id = {}
        self._data_layer_manager = None
        self._market_data_tokens: Dict[str, DataSubscriptionToken] = {}

        # Positions cache to smooth transient portfolio emptiness (e.g., after reconnect)
        # 防止IBKR偶发返回空portfolio快照导致前端仓位闪烁或误判
        self.positions_cache = []
        self.positions_cache_timestamp = None
        self.positions_cache_ttl_seconds = 15

        # Cache the last successful account snapshot for downstream fallbacks
        self._cached_account_info: Dict[str, Any] = {}
        self._cached_account_info_timestamp: Optional[datetime] = None

        # Counter for simulated order identifiers when trading is disabled
        self._simulated_order_id = itertools.count(start=-1, step=-1)

        # --- Connection monitoring & resilience state ---
        self._async_loop: Optional[asyncio.AbstractEventLoop] = None
        self._connection_monitor_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self._disconnect_time: Optional[datetime] = None
        self._reconnect_attempt: int = 0
        self._gateway_restart_triggered: bool = False
        self._self_restart_triggered: bool = False
        self._needs_cleanup: bool = False

        self._monitor_interval_seconds = max(1.0, self._parse_env_float('IBKR_CONNECTION_MONITOR_INTERVAL', 5.0))
        base_delay = self._parse_env_float('IBKR_RECONNECT_BASE_DELAY', 5.0) or 5.0
        self._reconnect_base_delay = max(0.5, base_delay)
        self._reconnect_max_delay = max(self._reconnect_base_delay, self._parse_env_float('IBKR_RECONNECT_MAX_DELAY', 60.0))
        self._gateway_restart_timeout = max(30.0, self._parse_env_float('IBKR_GATEWAY_RESTART_TIMEOUT', 120.0))
        self._self_restart_timeout = max(self._gateway_restart_timeout + 30.0, self._parse_env_float('IBKR_SELF_RESTART_TIMEOUT', 300.0))
        self._gateway_restart_cmd = os.getenv('IBKR_GATEWAY_RESTART_CMD')
        self._gateway_service_name = os.getenv('IBKR_GATEWAY_SERVICE_NAME', 'ib-gateway')

        self._setup_async_components()
    
    def _sanitize_float(self, value):
        """清理无效的浮点数值，将NaN和Infinity转换为None"""
        if value is None:
            return None
        try:
            if math.isnan(value) or math.isinf(value):
                return None
            return float(value)
        except (TypeError, ValueError):
            return None
    
    @property
    def connected(self) -> bool:
        """检查连接状态 - 为兼容性提供connected属性"""
        self._setup_async_components()
        return self.connection_manager.is_connected()
        
    def connect(self) -> bool:
        """连接到IBKR"""
        self._setup_async_components()
        result = self.connection_manager.connect()
        if result:
            self._handle_connection_restored()
        return result
    
    def disconnect(self):
        """断开连接"""
        self._schedule_market_data_cleanup()
        self.connection_manager.disconnect()

    def is_connected(self) -> bool:
        """检查连接状态"""
        self._setup_async_components()
        return self.connection_manager.is_connected()

    @property
    def ib(self) -> Optional[IB]:
        """获取IB实例"""
        return self.connection_manager.get_ib_instance()

    def _return_cached_account_info(self) -> Dict[str, Any]:
        """Return the most recent account snapshot if we have one cached."""
        if getattr(self, "_cached_account_info", None):
            return dict(self._cached_account_info)
        return {}

    # ------------------------------------------------------------------
    # Connection monitoring & resilience helpers

    def _setup_async_components(self) -> None:
        """Ensure we capture the running event loop and start monitor tasks."""

        loop = self._async_loop
        if loop is None or loop.is_closed():
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                return
            self._async_loop = loop

        if loop.is_closed():
            return

        try:
            loop.call_soon_threadsafe(self._start_connection_monitor_task)
        except RuntimeError:
            if loop.is_running():
                self._start_connection_monitor_task()

    def _start_connection_monitor_task(self) -> None:
        loop = self._async_loop
        if loop is None or loop.is_closed():
            return
        if self._connection_monitor_task and not self._connection_monitor_task.done():
            return
        self._connection_monitor_task = loop.create_task(
            self._connection_monitor_loop(),
            name="ibkr-connection-monitor",
        )

    async def _connection_monitor_loop(self) -> None:
        """Background loop that keeps an eye on connection state."""

        ibkr_logger.debug(
            "IBKR connection monitor started", interval=self._monitor_interval_seconds
        )

        previous_state = False
        try:
            previous_state = self.is_connected()
        except Exception as exc:
            ibkr_logger.debug(f"Initial connection check failed: {exc}")
            previous_state = False

        if previous_state:
            self._handle_connection_restored(initial=True)
        else:
            self._handle_disconnect_detected("monitor-start")
            await self._ensure_reconnect_task()

        try:
            while True:
                try:
                    await asyncio.sleep(self._monitor_interval_seconds)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    ibkr_logger.debug(f"Monitor sleep interrupted: {exc}")
                    continue

                try:
                    connected = self.is_connected()
                except Exception as exc:
                    ibkr_logger.warning(f"IBKR connection check failed: {exc}")
                    connected = False

                if connected:
                    if not previous_state:
                        self._handle_connection_restored()
                    previous_state = True
                    continue

                # Connection is down
                if previous_state:
                    self._handle_disconnect_detected("connection-lost")
                await self._ensure_reconnect_task()
                previous_state = False
        except asyncio.CancelledError:
            ibkr_logger.debug("IBKR connection monitor cancelled")
            raise

    def _handle_disconnect_detected(self, trigger: str) -> None:
        now = now_local()
        if self._disconnect_time is None:
            self._disconnect_time = now
            self._reconnect_attempt = 0
            self._gateway_restart_triggered = False
            self._self_restart_triggered = False
            self._needs_cleanup = True
            ibkr_logger.warning(
                "IBKR connection lost; starting auto-reconnect",
                trigger=trigger,
            )
        else:
            ibkr_logger.debug(
                "IBKR connection still unavailable",
                trigger=trigger,
                attempts=self._reconnect_attempt,
            )

    def _handle_connection_restored(self, *, initial: bool = False) -> None:
        if self._disconnect_time is not None and not initial:
            elapsed = (now_local() - self._disconnect_time).total_seconds()
            ibkr_logger.info(
                "IBKR connection restored",
                downtime_seconds=round(elapsed, 1),
                attempts=self._reconnect_attempt,
            )
        elif not initial:
            ibkr_logger.info("IBKR connection confirmed active")

        self._disconnect_time = None
        self._reconnect_attempt = 0
        self._gateway_restart_triggered = False
        self._self_restart_triggered = False
        self._needs_cleanup = False
        self._self_restart_suppressed_logged = False

        task = self._reconnect_task
        if task and not task.done():
            loop = self._async_loop
            try:
                if loop and not loop.is_closed():
                    loop.call_soon_threadsafe(task.cancel)
                else:
                    task.cancel()
            except RuntimeError:
                task.cancel()
        self._reconnect_task = None

    async def _ensure_reconnect_task(self) -> None:
        task = self._reconnect_task
        if task and not task.done():
            return
        self._reconnect_task = asyncio.create_task(
            self._reconnect_loop(),
            name="ibkr-reconnect-loop",
        )

    def _compute_backoff_delay(self, attempt_index: int) -> float:
        if attempt_index <= 0:
            return 0.0
        delay = self._reconnect_base_delay * (2 ** max(0, attempt_index - 1))
        return min(delay, self._reconnect_max_delay)

    async def _reconnect_loop(self) -> None:
        ibkr_logger.debug("IBKR reconnect loop started")
        try:
            while True:
                if self.is_connected():
                    return

                delay = self._compute_backoff_delay(self._reconnect_attempt)
                if delay > 0:
                    ibkr_logger.debug(
                        "Waiting before reconnect attempt",
                        delay_seconds=round(delay, 1),
                        attempt=self._reconnect_attempt + 1,
                    )
                    try:
                        await asyncio.sleep(delay)
                    except asyncio.CancelledError:
                        raise

                if self.is_connected():
                    return

                attempt_number = self._reconnect_attempt + 1
                ibkr_logger.info(
                    "Attempting IBKR reconnection",
                    attempt=attempt_number,
                )
                success = await self._attempt_connection(attempt_number)
                if success:
                    self._reconnect_attempt = 0
                    self._handle_connection_restored()
                    return

                self._reconnect_attempt = attempt_number
                await self._after_failed_attempt()
        except asyncio.CancelledError:
            ibkr_logger.debug("IBKR reconnect loop cancelled")
            raise

    async def _attempt_connection(self, attempt_number: int) -> bool:
        if self._needs_cleanup:
            try:
                await asyncio.to_thread(self.connection_manager.disconnect)
            except Exception as exc:
                ibkr_logger.debug(f"Cleanup disconnect failed: {exc}")
            finally:
                self._needs_cleanup = False

        try:
            result = await asyncio.to_thread(self.connection_manager.connect)
        except Exception as exc:
            ibkr_logger.error(
                "Reconnect attempt raised error",
                attempt=attempt_number,
                error=str(exc),
            )
            return False

        if result:
            return True

        ibkr_logger.warning(
            "Reconnect attempt did not succeed",
            attempt=attempt_number,
        )
        return False

    async def _after_failed_attempt(self) -> None:
        if self._disconnect_time is None:
            self._disconnect_time = now_local()

        elapsed = (now_local() - self._disconnect_time).total_seconds()
        ibkr_logger.warning(
            "IBKR reconnection still failing",
            attempts=self._reconnect_attempt,
            downtime_seconds=round(elapsed, 1),
        )

        if not self._gateway_restart_triggered and elapsed >= self._gateway_restart_timeout:
            await self._restart_ib_gateway_container(elapsed)

        if not self._self_restart_triggered and elapsed >= self._self_restart_timeout:
            if not self._process_restart_enabled:
                if not self._self_restart_suppressed_logged:
                    ibkr_logger.error(
                        "IBKR reconnect timeout exceeded but process restart is disabled; "
                        "set IBKR_ENABLE_PROCESS_RESTART=true to allow automatic restarts."
                    )
                    self._self_restart_suppressed_logged = True
                return
            self._self_restart_triggered = True
            reason = (
                f"IBKR reconnection failed for {int(elapsed)}s after {self._reconnect_attempt} attempts"
            )
            ibkr_logger.critical(reason)
            restart_program(reason=reason)

    async def _restart_ib_gateway_container(self, elapsed: float) -> None:
        self._gateway_restart_triggered = True
        ibkr_logger.warning(
            "Restarting IB Gateway container after prolonged disconnect",
            downtime_seconds=round(elapsed, 1),
        )

        command: Any
        shell = False

        if self._gateway_restart_cmd:
            command = self._gateway_restart_cmd
            shell = True
        else:
            compose_file = self.connection_manager._project_root / "docker" / "docker-compose.yml"
            if compose_file.exists():
                command = [
                    "docker",
                    "compose",
                    "-f",
                    str(compose_file),
                    "restart",
                    self._gateway_service_name,
                ]
            else:
                command = [
                    "docker",
                    "compose",
                    "restart",
                    self._gateway_service_name,
                ]

        success = await asyncio.to_thread(self._run_restart_command, command, shell)

        if not success and not self._gateway_restart_cmd:
            compose_file = self.connection_manager._project_root / "docker" / "docker-compose.yml"
            fallback_cmd = ["docker-compose"]
            if compose_file.exists():
                fallback_cmd.extend(["-f", str(compose_file)])
            fallback_cmd.extend(["restart", self._gateway_service_name])
            await asyncio.to_thread(self._run_restart_command, fallback_cmd, False)

        # Force a fresh session on next attempt
        self._needs_cleanup = True

    def _run_restart_command(self, command: Any, shell: bool) -> bool:
        try:
            result = subprocess.run(
                command,
                shell=shell,
                cwd=str(self.connection_manager._project_root),
                capture_output=True,
                text=True,
                timeout=180,
            )
        except FileNotFoundError as exc:
            ibkr_logger.error(f"Gateway restart command not found: {exc}")
            return False
        except Exception as exc:
            ibkr_logger.error(f"Gateway restart command failed: {exc}")
            return False

        if result.stdout:
            ibkr_logger.debug(
                "Gateway restart output",
                output=result.stdout.strip()[:500],
            )

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            ibkr_logger.error(
                "Gateway restart command exited with non-zero status",
                returncode=result.returncode,
                stderr=stderr[:500],
            )
            return False

        ibkr_logger.info("Gateway restart command completed successfully")
        return True
    
    def _place_order_in_thread(
        self,
        ib,
        symbol: str,
        quantity: int,
        action: str,
        exchange: str,
        sec_type: str,
        order_type: str,
        price: float = None,
    ):
        """在连接管理器的事件循环中执行下单操作，避免事件循环冲突。
        使用连接管理器的 run_async_in_connection_thread 方法确保正确执行。
        """
        import threading
        current_thread = threading.current_thread()
        ibkr_logger.info(f"_place_order_in_thread called for {symbol} in thread: {current_thread.name}")

        try:
            if is_trading_disabled():
                trade = self._simulate_trade(
                    order_type=order_type,
                    symbol=symbol,
                    quantity=quantity,
                    action=action,
                    exchange=exchange,
                    sec_type=sec_type,
                    price=price,
                )
                return trade

            # 检查连接状态
            if not self.connection_manager.is_connected():
                ibkr_logger.error("IBKR connection is not active")
                return None

            # 获取IB实例
            ib_instance = self.connection_manager.get_ib_instance()
            if not ib_instance:
                ibkr_logger.error("No active IB instance for placing order")
                return None

            # 构建异步下单流程
            async def execute_order_async():
                try:
                    # 根据证券类型获取正确的合约
                    if sec_type == 'FUT' or symbol.upper() in INDEX_FUTURE_SYMBOLS:
                        contract = self.get_future_contract(symbol, exchange)
                    else:
                        contract = self.get_stock_contract(symbol, exchange)

                    # 使用异步方式确认合约，避免在运行中事件循环上使用同步阻塞方法
                    qualified_contracts = await ib_instance.qualifyContractsAsync(contract)
                    if not qualified_contracts:
                        ibkr_logger.error(f"Failed to qualify contract for {symbol}")
                        return None

                    qualified_contract = qualified_contracts[0]

                    # 创建订单
                    if order_type == 'MARKET':
                        order = MarketOrder(action, quantity)
                    else:  # LIMIT
                        order = LimitOrder(action, quantity, price)

                    # 提交订单（placeOrder 为同步非阻塞方法，安全在事件循环线程调用）
                    trade = ib_instance.placeOrder(qualified_contract, order)
                    ibkr_logger.info(
                        f"{order_type} order placed: {action} {quantity} {symbol} ({sec_type}) on {qualified_contract.exchange}, "
                        f"conId={getattr(qualified_contract, 'conId', 'N/A')}"
                    )
                    return trade
                except Exception as e:
                    ibkr_logger.error(f"Failed to place order in async execution: {e}")
                    return None

            # 使用连接管理器的方法在正确的事件循环中执行
            try:
                return self.connection_manager.run_async_in_connection_thread(execute_order_async(), timeout=45)
            except Exception as e:
                ibkr_logger.error(f"Failed to place {order_type.lower()} order via connection manager: {e}")
                return None

        except Exception as e:
            ibkr_logger.error(f"Failed to place {order_type.lower()} order in thread: {e}")
            return None

    def _simulate_trade(
        self,
        order_type: str,
        symbol: str,
        quantity: int,
        action: str,
        exchange: str,
        sec_type: str,
        price: Optional[float] = None,
    ) -> SimpleNamespace:
        """Create a fake trade result when trading is disabled.

        Tests rely on the returned object exposing ``order.orderId`` similarly
        to an ``ib_insync`` ``Trade``.  We therefore mimic that minimal
        structure using :class:`types.SimpleNamespace` instances.
        """

        order_id = next(self._simulated_order_id)
        order = SimpleNamespace(
            orderId=order_id,
            action=action,
            totalQuantity=quantity,
            orderType=order_type,
            lmtPrice=price,
        )
        status = SimpleNamespace(status="Simulated", filled=0, remaining=quantity)
        contract = SimpleNamespace(symbol=symbol, exchange=exchange, secType=sec_type)
        trade = SimpleNamespace(order=order, orderStatus=status, contract=contract)

        display_price = price if price is not None else "MKT"
        ibkr_logger.warning(
            "Trading disabled; simulated {} order: {} {} {} @{} (order_id={})",
            order_type,
            action,
            quantity,
            symbol,
            display_price,
            order_id,
        )
        return trade
    
    def get_stock_contract(self, symbol: str, exchange: str = 'SMART', currency: str = 'USD') -> Stock:
        """获取股票合约"""
        return Stock(symbol, exchange, currency)

    def _create_future_contract(self, symbol: str, exchange: str, currency: str) -> Future:
        """Construct a future contract while tolerating minimal stubs.

        Some lightweight fallbacks (used in tests) expose ``Future`` objects
        without accepting constructor arguments.  When that happens we create an
        empty instance and populate the relevant attributes manually so that
        downstream code can rely on the expected fields being present.
        """

        try:
            return Future(symbol=symbol, exchange=exchange, currency=currency)
        except TypeError:
            contract = Future()
            for attr, value in (("symbol", symbol), ("exchange", exchange), ("currency", currency)):
                try:
                    setattr(contract, attr, value)
                except Exception:
                    pass
            if getattr(contract, "secType", None) in (None, ""):
                try:
                    setattr(contract, "secType", "FUT")
                except Exception:
                    pass
            return contract

    def get_future_contract(self, symbol: str, exchange: str = 'SMART', currency: str = 'USD'):
        """获取期货合约"""

        symbol_upper = symbol.upper()
        if symbol_upper in INDEX_FUTURE_SYMBOLS:
            now = now_local()
            contract_year, contract_month = _front_quarter_contract(now)
            expiry = f"{contract_year}{contract_month:02d}"

            details = INDEX_FUTURE_DETAILS[symbol_upper]
            exchange_to_use = details.get("exchange", exchange)

            contract = self._create_future_contract(symbol_upper, exchange_to_use, currency)
            contract.lastTradeDateOrContractMonth = expiry

            for attr in ("multiplier", "tradingClass"):
                value = details.get(attr)
                if value:
                    setattr(contract, attr, value)

            return contract

        return self._create_future_contract(symbol_upper, exchange, currency)
    
    def get_contract(self, symbol: str, sec_type: str = 'STK', exchange: str = 'SMART', currency: str = 'USD'):
        """根据证券类型获取合约"""
        if sec_type == 'FUT':
            return self.get_future_contract(symbol, exchange, currency)
        else:
            return self.get_stock_contract(symbol, exchange, currency)
    
    async def get_historical_data(self, symbol: str, duration: str = '1 Y',
                                bar_size: str = '1 day', exchange: str = 'SMART') -> pd.DataFrame:
        """获取历史数据（在连接线程事件循环中执行，避免事件循环错误）"""
        try:
            # 获取IB实例
            ib = self.ib
            if not ib or not ib.isConnected():
                ibkr_logger.error(f"IBKR connection lost for historical data: {symbol}")
                return pd.DataFrame()

            # MNQ是期货合约
            if symbol.upper() in INDEX_FUTURE_SYMBOLS:
                contract = self.get_future_contract(symbol, 'CME')
            else:
                contract = self.get_stock_contract(symbol, exchange)

            qualified = await ib.qualifyContractsAsync(contract)
            if not qualified:
                ibkr_logger.error(f"Failed to qualify contract for {symbol}")
                return None
            qc = qualified[0]
            # 使用异步版本请求历史数据
            bars = await ib.reqHistoricalDataAsync(
                contract=contract, endDateTime='', durationStr=duration,
                barSizeSetting=bar_size, whatToShow='TRADES', useRTH=False
            )
            if not bars:
                ibkr_logger.warning(f"No historical data returned for {symbol}")
                return pd.DataFrame()

            data = []
            for bar in bars:
                data.append({
                    'date': bar.date,
                    'open': bar.open,
                    'high': bar.high,
                    'low': bar.low,
                    'close': bar.close,
                    'volume': bar.volume
                })

            df = pd.DataFrame(data)
            ibkr_logger.info(f"Retrieved {len(df)} historical data points for {symbol}")
            return df
        except Exception as e:
            ibkr_logger.error(f"Failed to get historical data for {symbol}: {e}")
            return pd.DataFrame()
    
    def _ensure_backup_directory(self):
        """确保备份目录存在"""
        # 注释掉备份功能
        # os.makedirs(os.path.dirname(self.order_backup_file), exist_ok=True)
        pass
    
    def _backup_order(self, symbol: str, quantity: int, action: str, order_type: str, price: float = None, sec_type: str = 'STK', exchange: str = 'SMART'):
        """备份订单信息到CSV文件（已禁用）"""
        # 注释掉备份功能，不再创建备份文件
        pass
        # try:
        #     self._ensure_backup_directory()
        #     
        #     file_exists = os.path.exists(self.order_backup_file)
        #     
        #     with open(self.order_backup_file, 'a', newline='', encoding='utf-8') as csvfile:
        #         fieldnames = ['timestamp', 'symbol', 'quantity', 'action', 'order_type', 'price', 'sec_type', 'exchange', 'status']
        #         writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        #         
        #         if not file_exists:
        #             writer.writeheader()
        #         
        #         writer.writerow({
        #             'timestamp': now_local().isoformat(),
        #             'symbol': symbol,
        #             'quantity': quantity,
        #             'action': action,
        #             'order_type': order_type,
        #             'price': price,
        #             'sec_type': sec_type,
        #             'exchange': exchange,
        #             'status': 'PENDING'
        #         })
        # except Exception as e:
        #     ibkr_logger.error(f"Error backing up order: {e}")

    def _update_order_status(self, symbol: str, action: str, quantity: int, status: str):
        """更新备份文件中的订单状态（已禁用）"""
        # 注释掉备份功能
        pass
        # if not os.path.exists(self.order_backup_file):
        #     return
        #     
        # try:
        #     rows = []
        #     with open(self.order_backup_file, 'r', newline='', encoding='utf-8') as csvfile:
        #         reader = csv.DictReader(csvfile)
        #         for row in reader:
        #             if (row['symbol'] == symbol and 
        #                 row['action'] == action and 
        #                 int(row['quantity']) == quantity and 
        #                 row['status'] == 'PENDING'):
        #                 row['status'] = status
        #             rows.append(row)
        #     
        #     with open(self.order_backup_file, 'w', newline='', encoding='utf-8') as csvfile:
        #         if rows:
        #             fieldnames = rows[0].keys()
        #             writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        #             writer.writeheader()
        #             writer.writerows(rows)
        # except Exception as e:
        #     ibkr_logger.error(f"Error updating order status: {e}")

    def get_order_backup_history(self, days: int = 7) -> List[Dict]:
        """获取订单备份历史（已禁用）"""
        # 注释掉备份功能，返回空列表
        return []
        # try:
        #     if not os.path.exists(self.order_backup_file):
        #         return []
        #     
        #     cutoff_date = now_local() - timedelta(days=days)
        #     orders = []
        #     
        #     with open(self.order_backup_file, 'r', newline='', encoding='utf-8') as csvfile:
        #         reader = csv.DictReader(csvfile)
        #         for row in reader:
        #             order_date = datetime.fromisoformat(row['timestamp'])
        #             if order_date >= cutoff_date:
        #                 orders.append(row)
        #     
        #     return orders
        # except Exception as e:
        #     ibkr_logger.error(f"Error reading backup history: {e}")
        #     return []

    def restore_pending_orders(self) -> List[Dict]:
        """恢复待处理的订单（已禁用）"""
        # 注释掉备份功能，返回空列表
        return []
        # try:
        #     if not os.path.exists(self.order_backup_file):
        #         return []
        #     
        #     pending_orders = []
        #     
        #     with open(self.order_backup_file, 'r', newline='', encoding='utf-8') as csvfile:
        #         reader = csv.DictReader(csvfile)
        #         for row in reader:
        #             if row['status'] == 'PENDING':
        #                 pending_orders.append(row)
        #                 # 标记为过期
        #                 self._update_order_status(row['symbol'], row['action'], int(row['quantity']), 'EXPIRED')
        #     
        #     return pending_orders
        # except Exception as e:
        #     ibkr_logger.error(f"Error restoring pending orders: {e}")
        #     return []

    def save_historical_data(self, df: pd.DataFrame, symbol: str, filename: str = None):
        """保存历史数据到数据库"""
        try:
            # 获取数据库管理器
            db_manager = get_db_manager()
            
            # 保存到数据库
            db_manager.save_historical_data(symbol, df)
            ibkr_logger.info(f"Historical data for {symbol} saved to database")
            
            # 保留文件保存作为备份（可选）
            if filename:
                os.makedirs(os.path.dirname(filename), exist_ok=True)
                df.to_csv(filename, index=False)
                ibkr_logger.info(f"Historical data also saved to {filename}")
        except Exception as e:
            ibkr_logger.error(f"Error saving historical data to database: {e}")
            # 回退到文件保存
            if filename is None:
                filename = f"data/{symbol}_historical.csv"
            
            os.makedirs(os.path.dirname(filename), exist_ok=True)
            df.to_csv(filename, index=False)
            ibkr_logger.info(f"Historical data saved to {filename} (fallback)")
    
    async def get_real_time_data(self, symbol: str, exchange: str = 'SMART'):
        """获取实时行情，支持统一数据层或回退旧实现。"""

        mode = get_data_layer_mode()
        if is_legacy_mode(mode):
            return await self._get_real_time_data_legacy(symbol, exchange)

        try:
            result = await self._get_real_time_data_unified(symbol, exchange)
            if result or not is_dual_mode(mode):
                return result
            ibkr_logger.debug(
                "Unified real-time data empty, falling back to legacy",
                symbol=symbol,
                exchange=exchange,
            )
        except DataSourceError as exc:
            ibkr_logger.warning(
                "Unified real-time data unavailable: {}", exc, symbol=symbol
            )
        except Exception as exc:
            ibkr_logger.exception("Unified real-time data error: {}", exc)

        return await self._get_real_time_data_legacy(symbol, exchange)

    async def _get_real_time_data_legacy(self, symbol: str, exchange: str = 'SMART'):
        """获取实时数据 - 兼容旧实现"""
        import threading
        current_thread = threading.current_thread()
        ibkr_logger.debug(f"get_real_time_data called for {symbol} in thread: {current_thread.name}")
        
        try:
            symbol_upper = symbol.upper()
            is_index_future = symbol_upper in INDEX_FUTURE_SYMBOLS

            # 1. 首先检查缓存，提供快速响应
            if symbol in self.price_cache:
                cached_time = self.price_cache_timestamp.get(symbol)
                if cached_time and (now_local() - cached_time).total_seconds() < self.price_cache_ttl_seconds:
                    ibkr_logger.debug(f"Returning cached data for {symbol}")
                    return {
                        'symbol': symbol,
                        'last': self._sanitize_float(self.price_cache[symbol]),
                        'bid': 0,
                        'ask': 0,
                        'volume': 0,
                        'timestamp': to_local_time(datetime.now(NY_TZ)),
                        'is_cached': True
                    }
            
            # 2. 确保连接
            ibkr_logger.debug(f"Checking IBKR connection for {symbol}")
            if not self.is_connected():
                ibkr_logger.warning(f"IBKR not connected; skipping real-time data request for {symbol}")
                return None
            
            # 3. 获取IB实例
            ib = self.ib
            if not ib or not ib.isConnected():
                ibkr_logger.error(f"IBKR connection lost for real-time data: {symbol}")
                return None
            
            # 4. 创建合约
            if is_index_future:
                ibkr_logger.debug(f"Creating future contract for {symbol}")
                contract = self.get_future_contract(symbol_upper, 'CME')
            else:
                ibkr_logger.debug(f"Creating stock contract for {symbol}")
                contract = self.get_stock_contract(symbol, exchange)
            
            # 5. 在连接线程请求市场数据（快速返回ticker对象，不阻塞连接线程）
            def _req_ticker():
                try:
                    return ib.reqMktData(contract, '', False, False)
                except Exception as e:
                    ibkr_logger.error(f"Error requesting market data for {symbol} in connection thread: {e}")
                    return None
            
            ticker = await asyncio.to_thread(
                self.connection_manager.run_in_connection_thread,
                _req_ticker,
                timeout=10.0,
            )
            if not ticker:
                ibkr_logger.error(f"Failed to obtain ticker for {symbol}")
                return None
            
            # 6. 非阻塞方式检查数据，避免阻塞主线程
            # 对于指数期货合约，增加等待时间
            max_wait = 3.0 if is_index_future else 1.0  # 减少最大等待时间
            wait_interval = 0.1  # 减少检查间隔
            waited = 0.0
            result_payload = None
            
            # 立即检查一次数据是否已经可用
            last = getattr(ticker, 'last', 0) or 0
            bid = getattr(ticker, 'bid', 0) or 0
            ask = getattr(ticker, 'ask', 0) or 0
            volume = getattr(ticker, 'volume', 0) or 0

            if last and last > 0:
                ibkr_logger.debug(f"Retrieved real-time data for {symbol}: last={last} (immediate)")
                self.price_cache[symbol] = last
                self.price_cache_timestamp[symbol] = now_local()
                ts = getattr(ticker, 'time', None) or datetime.now(NY_TZ)
                result_payload = {
                    'symbol': symbol,
                    'last': self._sanitize_float(last),
                    'bid': self._sanitize_float(bid),
                    'ask': self._sanitize_float(ask),
                    'volume': self._sanitize_float(volume),
                    'timestamp': to_local_time(ts)
                }
            elif bid and ask and bid > 0 and ask > 0:
                mid = (bid + ask) / 2
                ibkr_logger.debug(f"Retrieved bid/ask mid for {symbol}: mid={mid:.2f} (immediate)")
                self.price_cache[symbol] = mid
                self.price_cache_timestamp[symbol] = now_local()
                ts = getattr(ticker, 'time', None) or datetime.now(NY_TZ)
                result_payload = {
                    'symbol': symbol,
                    'last': self._sanitize_float(mid),
                    'bid': self._sanitize_float(bid),
                    'ask': self._sanitize_float(ask),
                    'volume': self._sanitize_float(volume),
                    'timestamp': to_local_time(ts)
                }
            else:
                # 如果立即没有数据，进行短暂的非阻塞等待
                while waited < max_wait and not result_payload:
                    await asyncio.sleep(wait_interval)
                    waited += wait_interval
                    last = getattr(ticker, 'last', 0) or 0
                    bid = getattr(ticker, 'bid', 0) or 0
                    ask = getattr(ticker, 'ask', 0) or 0
                    volume = getattr(ticker, 'volume', 0) or 0
                    
                    if last and last > 0:
                        ibkr_logger.debug(f"Retrieved real-time data for {symbol}: last={last} (waited {waited:.1f}s)")
                        self.price_cache[symbol] = last
                        self.price_cache_timestamp[symbol] = now_local()
                        ts = getattr(ticker, 'time', None) or datetime.now(NY_TZ)
                        result_payload = {
                            'symbol': symbol,
                            'last': self._sanitize_float(last),
                            'bid': self._sanitize_float(bid),
                            'ask': self._sanitize_float(ask),
                            'volume': self._sanitize_float(volume),
                            'timestamp': to_local_time(ts)
                        }
                        break

                    if bid and ask and bid > 0 and ask > 0:
                        mid = (bid + ask) / 2
                        ibkr_logger.debug(f"Retrieved bid/ask mid for {symbol}: mid={mid:.2f} (waited {waited:.1f}s)")
                        self.price_cache[symbol] = mid
                        self.price_cache_timestamp[symbol] = now_local()
                        ts = getattr(ticker, 'time', None) or datetime.now(NY_TZ)
                        result_payload = {
                            'symbol': symbol,
                            'last': self._sanitize_float(mid),
                            'bid': self._sanitize_float(bid),
                            'ask': self._sanitize_float(ask),
                            'volume': self._sanitize_float(volume),
                            'timestamp': to_local_time(ts)
                        }
                        break
            
            # 7. 取消市场数据订阅以避免泄漏（在连接线程中执行）
            def _cancel_mkt_data():
                try:
                    ib.cancelMktData(contract)
                except Exception as e:
                    ibkr_logger.warning(f"Failed to cancel market data for {symbol}: {e}")
            await asyncio.to_thread(
                self.connection_manager.run_in_connection_thread,
                _cancel_mkt_data,
                timeout=5.0,
            )
            
            if result_payload:
                return result_payload
            
            ibkr_logger.warning(f"No market data received for {symbol} after {max_wait}s")
            # 7b. fallback 返回缓存
            if symbol in self.price_cache:
                cached_price = self.price_cache[symbol]
                ibkr_logger.info(f"No fresh data for {symbol}, returning cached: {cached_price}")
                return {
                    'symbol': symbol,
                    'last': self._sanitize_float(cached_price),
                    'bid': 0,
                    'ask': 0,
                    'volume': 0,
                    'timestamp': datetime.now(NY_TZ),
                    'is_cached': True
                }
            
            return None
            
        except Exception as e:
            ibkr_logger.error(f"Exception in get_real_time_data for {symbol}: {e}")
            # 最后的fallback
            if symbol in self.price_cache:
                cached_price = self.price_cache[symbol]
                ibkr_logger.info(f"Exception occurred for {symbol}, returning cached: {cached_price}")
                return {
                    'symbol': symbol,
                    'last': self._sanitize_float(cached_price),
                    'bid': 0,
                    'ask': 0,
                    'volume': 0,
                    'timestamp': to_local_time(datetime.now(NY_TZ)),
                    'is_cached': True,
                    'error': str(e)
                }
            return None

    async def _get_real_time_data_unified(self, symbol: str, exchange: str = 'SMART') -> Optional[Dict[str, Any]]:
        manager = self._ensure_data_layer_manager()
        if manager is None:
            return None

        symbol_key = (symbol or '').strip().upper()
        if not symbol_key:
            return None

        options: Dict[str, Any] = {"exchange": exchange}
        if symbol_key in INDEX_FUTURE_SYMBOLS:
            options["sec_type"] = "FUT"

        request = DataSubscriptionRequest(
            channel="market.tick",
            symbol=symbol_key,
            options=options,
        )

        token = self._market_data_tokens.get(symbol_key)
        if token is None:
            token = await manager.subscribe(request)
            self._market_data_tokens[symbol_key] = token

        snapshot = await manager.snapshot(request)
        if not snapshot:
            await asyncio.sleep(0.1)
            snapshot = await manager.snapshot(request)
            if not snapshot:
                return None

        ts_ns = snapshot.get("ts_ns")
        timestamp_iso: Optional[str]
        if ts_ns is not None:
            try:
                ts_int = int(ts_ns)
            except (TypeError, ValueError):
                ts_int = None
            if ts_int is not None:
                ts_dt = datetime.fromtimestamp(ts_int / 1_000_000_000, tz=timezone.utc)
                timestamp_iso = to_local_time(ts_dt)
            else:
                timestamp_iso = to_local_time(datetime.now(NY_TZ))
        else:
            timestamp_iso = to_local_time(datetime.now(NY_TZ))

        result = {
            'symbol': symbol,
            'last': self._sanitize_float(snapshot.get('last')),
            'bid': self._sanitize_float(snapshot.get('bid')),
            'ask': self._sanitize_float(snapshot.get('ask')),
            'volume': self._sanitize_float(snapshot.get('volume')),
            'timestamp': timestamp_iso,
            'market_state': snapshot.get('market_state'),
            'source': 'unified',
        }
        if ts_ns is not None:
            try:
                result['ts_ns'] = int(ts_ns)
            except (TypeError, ValueError):
                pass

        self.price_cache[symbol] = result['last']
        self.price_cache_timestamp[symbol] = now_local()
        return result

    def _ensure_data_layer_manager(self):
        if self._data_layer_manager is None:
            try:
                self._data_layer_manager = get_data_source_manager()
            except Exception as exc:  # pragma: no cover - defensive guard
                ibkr_logger.warning("Failed to initialise data source manager: {}", exc)
                return None
        return self._data_layer_manager

    async def _release_market_data_subscriptions(self) -> None:
        if not self._market_data_tokens:
            return
        manager = self._data_layer_manager
        tokens = list(self._market_data_tokens.items())
        self._market_data_tokens.clear()
        if not manager:
            return
        for symbol_key, token in tokens:
            try:
                await manager.unsubscribe(token)
            except Exception as exc:  # pragma: no cover - defensive guard
                ibkr_logger.debug(
                    "Failed to unsubscribe market data token for {}: {}",
                    symbol_key,
                    exc,
                )

    def _schedule_market_data_cleanup(self) -> None:
        if not self._market_data_tokens:
            return

        async def _cleanup():
            await self._release_market_data_subscriptions()

        try:
            asyncio.run(_cleanup())
        except RuntimeError:
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                return
            if loop.is_running():
                loop.create_task(_cleanup())

    async def place_market_order(self, symbol: str, quantity: int,
                           action: str = 'BUY', exchange: str = 'SMART', sec_type: str = 'STK') -> Optional[Order]:
        """下市价单"""
        # 下单前备份订单信息
        # self._backup_order(symbol, quantity, action, 'MARKET', sec_type=sec_type, exchange=exchange)
        
        try:
            trade = self._place_order_in_thread(
                None,  # will use self.ib
                symbol, quantity, action, exchange, sec_type, 'MARKET'
            )
            
            if trade:
                # 记录reqId到订单ID的映射，用于错误处理
                req_id = trade.order.orderId
                order_id = str(req_id)
                self.req_id_to_order_id[req_id] = order_id
                ibkr_logger.debug(f"Mapped reqId {req_id} to order_id {order_id}")
                
                # 更新订单状态
                self._update_order_status(symbol, action, quantity, 'Submitted')
            
            return trade
        except Exception as e:
            ibkr_logger.error(f"Failed to place market order: {e}")
            return None

    def place_limit_order(self, symbol: str, quantity: int, price: float,
                              action: str = 'BUY', exchange: str = 'SMART', sec_type: str = 'STK') -> Optional[Order]:
        """下限价单"""
        # 下单前备份订单信息
        # self._backup_order(symbol, quantity, action, 'LIMIT', price, sec_type=sec_type, exchange=exchange)
        
        try:
            trade = self._place_order_in_thread(
                None,  # will use self.ib
                symbol, quantity, action, exchange, sec_type, 'LIMIT', price
            )
            
            if trade:
                # 记录reqId到订单ID的映射，用于错误处理
                req_id = trade.order.orderId
                order_id = str(req_id)
                self.req_id_to_order_id[req_id] = order_id
                ibkr_logger.debug(f"Mapped reqId {req_id} to order_id {order_id}")
                
                # 更新订单状态
                self._update_order_status(symbol, action, quantity, 'Submitted')
            
            return trade
        except Exception as e:
            ibkr_logger.error(f"Failed to place limit order: {e}")
            return None

    def refresh_orders(self) -> bool:
        """刷新IBKR订单数据，使用异步API避免事件循环冲突"""
        ib = self.ib
        if not ib or not ib.isConnected():
            ibkr_logger.error("IBKR connection lost for order refresh")
            return False

        try:
            async def _refresh():
                try:
                    await ib.reqOpenOrdersAsync()
                    await ib.reqAllOpenOrdersAsync()
                    await asyncio.sleep(0.2)
                    ibkr_logger.debug("Requested order refresh from IBKR (async)")
                    return True
                except Exception as e:
                    ibkr_logger.warning(f"Failed to refresh orders: {e}")
                    return False

            result = self.connection_manager.run_async_in_connection_thread(_refresh(), timeout=1.5)
            if result is not None:
                return bool(result)
            else:
                # 如果异步方法失败，回退到同步方法
                ibkr_logger.warning("Async order refresh failed, falling back to sync method")
                try:
                    ib.reqOpenOrders()
                    ib.reqAllOpenOrders()
                    import time
                    time.sleep(0.2)
                    ibkr_logger.debug("Requested order refresh from IBKR (sync fallback)")
                    return True
                except Exception as e:
                    ibkr_logger.error(f"Sync order refresh also failed: {e}")
                    return False
        except Exception as e:
            ibkr_logger.error(f"Order refresh failed: {e}")
            return False

    def cancel_order(self, order_id: Any) -> tuple[bool, str]:
        """取消指定订单"""
        # 规范化 order_id，内部与 IBKR 比较使用 int，外部广播/日志统一使用字符串
        order_id_str = str(order_id)
        if is_trading_disabled():
            ibkr_logger.warning(
                "Trading disabled; simulated cancellation acknowledged for order {}",
                order_id_str,
            )
            return True, "Cancelled (simulated)"

        try:
            order_id_int = int(order_id_str)
        except Exception:
            ibkr_logger.error(f"Invalid order_id for cancellation: {order_id}")
            return False, "Invalid order_id"
        try:
            # 确保连接
            if not self.is_connected():
                ibkr_logger.error(f"Cannot cancel order {order_id}: IBKR is not connected")
                return False, "Not connected"

            # 刷新订单数据，使用异步API避免事件循环冲突
            try:
                self.refresh_orders()
            except Exception as refresh_error:
                ibkr_logger.warning(f"Failed to refresh orders before cancel: {refresh_error}")
                # 继续执行取消操作，不因刷新失败而中断

            # 在连接线程事件循环中执行取消逻辑，避免跨线程事件循环错误
            async def _cancel_op():
                try:
                    ib_local = self.ib
                    if not ib_local or not ib_local.isConnected():
                        ibkr_logger.error("IBKR connection lost for cancel order")
                        return False, "Connection lost"

                    trades = ib_local.trades()
                    target_trade = None
                    for trade in trades:
                        if trade.order.orderId == order_id:
                            target_trade = trade
                            break

                    if not target_trade:
                        # 快速重查一次，避免因列表延迟导致误判
                        try:
                            await ib_local.reqOpenOrdersAsync()
                            await ib_local.reqAllOpenOrdersAsync()
                            await asyncio.sleep(0.2)
                            trades = ib_local.trades()
                            for trade in trades:
                                if trade.order.orderId == order_id:
                                    target_trade = trade
                                    break
                        except Exception as requery_err:
                            ibkr_logger.debug(f"Quick requery before cancel failed: {requery_err}")

                    if not target_trade:
                        ibkr_logger.info(f"Order {order_id} not found in active trades")
                        return False, f"Order {order_id} not found or already completed"

                    current_status = target_trade.orderStatus.status
                    ibkr_logger.info(f"Order {order_id} current status: {current_status}")

                    # 检查订单状态是否可以取消
                    if current_status in ['Filled', 'Cancelled', 'Inactive']:
                        ibkr_logger.warning(f"Order {order_id} cannot be cancelled, status: {current_status}")
                        return False, f"Order cannot be cancelled, current status: {current_status}"

                    # 如果已经是PendingCancel状态，短暂确认后直接返回；否则发送取消请求
                    if current_status == 'PendingCancel':
                        await asyncio.sleep(0.5)
                        ibkr_logger.info(f"Order {order_id} already in PendingCancel, acknowledging cancellation initiation")
                        return True, "Order cancellation initiated (PendingCancel)"
                    else:
                        ibkr_logger.info(f"Sending cancel request for order {order_id}")
                        try:
                            ib_local.cancelOrder(target_trade.order)
                        except Exception as send_err:
                            ibkr_logger.error(f"Error sending cancel request for order {order_id}: {send_err}")
                            return False, f"Error sending cancel request: {send_err}"

                    # 等待取消确认，使用更短超时与轮询间隔，并对 PendingCancel 早返回
                    max_wait_time = 10.0
                    check_interval = 0.25
                    start_time = asyncio.get_event_loop().time()
                    pending_first_seen_elapsed = None

                    while True:
                        elapsed = asyncio.get_event_loop().time() - start_time
                        if elapsed >= max_wait_time:
                            break
                        await asyncio.sleep(check_interval)

                        updated_trades = ib_local.trades()
                        found = False
                        for updated_trade in updated_trades:
                            if updated_trade.order.orderId == order_id:
                                found = True
                                new_status = updated_trade.orderStatus.status
                                if new_status in ['Cancelled', 'Inactive']:
                                    ibkr_logger.info(f"Order {order_id} successfully cancelled, final status: {new_status}")
                                    return True, f"Order cancelled successfully, status: {new_status}"
                                elif new_status == 'PendingCancel':
                                    if pending_first_seen_elapsed is None:
                                        pending_first_seen_elapsed = elapsed
                                        ibkr_logger.info(f"Order {order_id} pending cancellation...")
                                    elif elapsed - pending_first_seen_elapsed >= 1.0:
                                        ibkr_logger.info(f"Order {order_id} cancellation acknowledged (PendingCancel)")
                                        return True, "Order cancellation initiated (PendingCancel)"
                                else:
                                    if new_status != current_status:
                                        ibkr_logger.info(f"Order {order_id} status changed from {current_status} to {new_status}")
                                        current_status = new_status
                                break
                        if not found:
                            # 订单不在trades中了，在Paper Trading中通常意味着已取消
                            ibkr_logger.debug(f"Order {order_id} not found in current trades, rechecking...")

                    # 超时后最终检查
                    final_trades = ib_local.trades()
                    for final_trade in final_trades:
                        if final_trade.order.orderId == order_id:
                            final_status = final_trade.orderStatus.status
                            if final_status in ['Cancelled', 'Inactive']:
                                ibkr_logger.info(f"Order {order_id} finally cancelled, status: {final_status}")
                                return True, f"Order cancelled successfully, status: {final_status}"
                            elif final_status == 'PendingCancel':
                                ibkr_logger.warning(f"Order {order_id} still in PendingCancel after {max_wait_time}s - may be normal in Paper Trading")
                                return True, f"Order cancellation initiated (Paper Trading may take longer)"
                            else:
                                ibkr_logger.warning(f"Order {order_id} cancellation timeout, final status: {final_status}")
                                return False, f"Cancellation timeout, order status: {final_status}"

                    # 订单不在列表中，在Paper Trading中这通常意味着已取消
                    ibkr_logger.info(f"Order {order_id} no longer in trades list after timeout - likely cancelled in Paper Trading")
                    return True, "Order likely cancelled (no longer in active trades)"
                except Exception as inner_e:
                    ibkr_logger.error(f"Error during cancellation operation for order {order_id}: {inner_e}")
                    return False, f"Error during cancellation: {inner_e}"

            result = self.connection_manager.run_async_in_connection_thread(_cancel_op(), timeout=16)
            if result is not None:
                return result
            else:
                ibkr_logger.warning("Failed to execute cancel operation in connection thread")
                return False, "Failed to dispatch cancel operation"

        except Exception as e:
            ibkr_logger.error(f"Failed to cancel order {order_id}: {e}")
            return False, f"Error: {str(e)}"
    
    def cancel_all_orders(self) -> int:
        """取消所有订单"""
        def _cancel_orders_operation():
            """在连接线程中执行取消订单操作，避免事件循环警告"""
            try:
                # 获取IB实例
                ib = self.ib
                if not ib or not ib.isConnected():
                    ibkr_logger.error(f"IBKR connection lost for cancel all orders")
                    return 0

                # 优先使用openTrades()获取活跃订单，降级到trades()
                trades = ib.openTrades() or ib.trades()
                cancellable_orders = []
                
                # 过滤出可取消的订单，包括更多状态以确保捕获所有活跃订单
                cancellable_statuses = [
                    'Submitted', 'PreSubmitted', 'PendingSubmit', 'PendingCancel',
                    'ApiPending', 'ApiCancelled'
                ]
                
                # 取消所有当前可取消的订单（不再按 clientId 过滤）
                for trade in trades:
                    order_status = trade.orderStatus.status
                    order_id = trade.order.orderId
                    
                    # 记录所有活跃订单的状态以便调试
                    ibkr_logger.debug(f"Order {order_id} status: {order_status}")
                    
                    # 检查订单是否可取消
                    if order_status in cancellable_statuses:
                        cancellable_orders.append(trade)
                        ibkr_logger.info(f"Order {order_id} marked for cancellation (status: {order_status})")
                    elif order_status not in ['Filled', 'Cancelled', 'Inactive']:
                        # 处理未知状态，保守地尝试取消
                        ibkr_logger.warning(f"Order {order_id} has unknown status '{order_status}', attempting to cancel")
                        cancellable_orders.append(trade)
                
                if not cancellable_orders:
                    ibkr_logger.info("No cancellable orders found")
                    return 0
                
                ibkr_logger.info(f"Found {len(cancellable_orders)} orders to cancel")
                
                # 发送取消请求
                for trade in cancellable_orders:
                    try:
                        order_id = trade.order.orderId
                        ib.cancelOrder(trade.order)
                        ibkr_logger.info(f"Sent cancel request for order {order_id}")
                    except Exception as e:
                        ibkr_logger.error(f"Failed to send cancel request for order {trade.order.orderId}: {e}")
                
                # 等待取消确认
                max_wait_time = 20
                start_time = time.time()
                check_interval = 0.5
                cancelled_count = 0
                
                while time.time() - start_time < max_wait_time:
                    time.sleep(check_interval)
                    
                    # 重新获取订单状态
                    updated_trades = ib.trades()
                    current_cancelled = 0
                    
                    for original_trade in cancellable_orders:
                        order_id = original_trade.order.orderId
                        found = False
                        
                        for updated_trade in updated_trades:
                            if updated_trade.order.orderId == order_id:
                                found = True
                                status = updated_trade.orderStatus.status
                                if status in ['Cancelled', 'Inactive']:
                                    current_cancelled += 1
                                break
                        
                        # 订单不在列表中，在Paper Trading中通常意味着已取消
                        if not found:
                            current_cancelled += 1
                    
                    # 如果所有订单都已取消，提前退出
                    if current_cancelled >= len(cancellable_orders):
                        cancelled_count = current_cancelled
                        break
                    
                    # 更新已取消数量
                    if current_cancelled > cancelled_count:
                        cancelled_count = current_cancelled
                        ibkr_logger.info(f"Progress: {cancelled_count}/{len(cancellable_orders)} orders cancelled")
                
                # 最终检查
                if cancelled_count < len(cancellable_orders):
                    final_trades = ib.trades()
                    final_cancelled = 0
                    
                    for original_trade in cancellable_orders:
                        order_id = original_trade.order.orderId
                        found = False
                        
                        for final_trade in final_trades:
                            if final_trade.order.orderId == order_id:
                                found = True
                                status = final_trade.orderStatus.status
                                if status in ['Cancelled', 'Inactive', 'PendingCancel']:
                                    final_cancelled += 1
                                break
                        
                        if not found:
                            final_cancelled += 1
                    
                    cancelled_count = final_cancelled
                
                ibkr_logger.info(f"Successfully cancelled {cancelled_count}/{len(cancellable_orders)} orders")
                return cancelled_count
                
            except Exception as e:
                ibkr_logger.error(f"Error in cancel orders operation: {e}")
                return 0
        
        try:
            # 确保连接
            if not self.is_connected():
                ibkr_logger.error("Cannot cancel orders: IBKR is not connected")
                return 0

            # 刷新订单，使用异步API避免事件循环冲突
            try:
                self.refresh_orders()
            except Exception as refresh_error:
                ibkr_logger.warning(f"Failed to refresh orders before cancel all: {refresh_error}")
                # 继续执行取消操作，不因刷新失败而中断

            # 尝试使用连接管理器在正确的线程中执行操作
            result = self.connection_manager.run_in_connection_thread(
                _cancel_orders_operation,
                timeout=15.0,
            )
            
            if result is not None:
                return result
            else:
                # 回退到直接执行
                ibkr_logger.warning("Failed to use connection thread, falling back to direct execution")
                return _cancel_orders_operation()
                
        except Exception as e:
            ibkr_logger.error(f"Failed to cancel all orders: {e}")
            return 0
    
    def get_orders(self, risk_manager=None, strategy_manager=None, order_manager=None) -> List[Dict]:
        """获取订单列表（支持去抖动，避免重复刷新，并同步到 OrderManager）"""
        try:
            # 确保连接
            if not self.is_connected():
                ibkr_logger.error("Cannot retrieve orders: IBKR is not connected")
                return []
            
            ib = self.ib
            if not ib or not ib.isConnected():
                ibkr_logger.error(f"IBKR connection lost for get orders")
                return []

            # 添加去抖逻辑 - 避免在短时间内重复刷新
            current_time = time.time()
            if not hasattr(self, '_last_order_refresh_time'):
                self._last_order_refresh_time = 0
            
            # 如果距离上次刷新不足1秒，直接返回缓存的trades数据
            time_since_last_refresh = current_time - self._last_order_refresh_time
            if time_since_last_refresh < 1.0:
                ibkr_logger.debug(f"Skipping order refresh, last refresh was {time_since_last_refresh:.1f}s ago")
                # 直接使用缓存的开放订单，不重复请求
                trades = ib.openTrades()
            else:
                # 刷新订单数据并更新刷新时间
                self.refresh_orders()
                self._last_order_refresh_time = current_time
                # 读取最新的开放订单列表
                trades = ib.openTrades()
            
            # 处理订单数据
            orders: List[Dict[str, Any]] = []
            updated_orders = False  # Track if any orders were updated
            changed_orders: List[tuple[str, str]] = []  # (order_id_str, new_status)
            
            for trade in trades:
                # 检查orderId是否有效，跳过ID为0的订单
                order_id = trade.order.orderId
                if order_id == 0 or order_id is None:
                    ibkr_logger.warning(
                        f"⚠️ 跳过无效订单ID: orderId={order_id}, "
                        f"symbol={getattr(trade.contract, 'symbol', 'Unknown')}, "
                        f"action={trade.order.action}, quantity={trade.order.totalQuantity}. "
                        f"无法确定对应的本地订单，跳过更新以避免错误覆盖。"
                    )
                    continue
                
                # 检查剩余数量，跳过已完成的订单（剩余数量为0）
                remaining_qty = getattr(trade.order, 'totalQuantity', 0) - getattr(trade, 'filled', 0)
                if remaining_qty <= 0:
                    ibkr_logger.debug(
                        f"跳过已完成订单: orderId={order_id}, "
                        f"symbol={getattr(trade.contract, 'symbol', 'Unknown')}, "
                        f"totalQty={trade.order.totalQuantity}, filled={getattr(trade, 'filled', 0)}"
                    )
                    continue
                
                # 从risk_manager的auto_orders中获取订单来源信息
                order_source = "未知"
                order_id_str = str(order_id)
                
                # 先尝试从 OrderManager 中获取订单
                existing_order = None
                if order_manager:
                    existing_order = order_manager.get_order(order_id_str)
                
                # 如果未找到现有订单，尝试基于根符号/动作/数量进行匹配，并修正主键
                if order_manager and not existing_order:
                    try:
                        matched_order = order_manager.find_potential_match(
                            symbol=getattr(trade.contract, 'localSymbol', None) or trade.contract.symbol,
                            sec_type=getattr(trade.contract, 'secType', 'STK'),
                            action=trade.order.action,
                            quantity=trade.order.totalQuantity,
                            time_window_seconds=300
                        )
                        if matched_order and matched_order.order_id != order_id_str:
                            # 将内存和数据库中的旧订单ID更正为IB返回的orderId
                            old_order_id = matched_order.order_id
                            if order_manager.rekey_order_id(old_order_id, order_id_str):
                                existing_order = order_manager.get_order(order_id_str)
                                ibkr_logger.info(f"Re-keyed matched order {old_order_id} -> {order_id_str} by root-symbol match")
                    except Exception as match_err:
                        ibkr_logger.warning(f"Failed to match/rekey order for IB order {order_id_str}: {match_err}")
                
                if risk_manager and hasattr(risk_manager, 'auto_orders'):
                    if order_id_str in risk_manager.auto_orders:
                        auto_order = risk_manager.auto_orders[order_id_str]
                        raw_src = getattr(auto_order, 'order_source', None) or "auto"
                        if raw_src in ("manual", "auto", "strategy", "tp", "sl", "bot"):
                            detected_source_code = raw_src
                        else:
                            detected_source_code = "manual"
                        if detected_source_code == "manual":
                            order_source = "人工"
                        elif detected_source_code == "auto":
                            order_source = "机器人"
                        else:
                            order_source = detected_source_code
                        
                        # Synchronize order data back to risk_manager if status or price changed
                        current_status = trade.orderStatus.status
                        current_avg_fill_price = trade.orderStatus.avgFillPrice
                        
                        if (auto_order.status != current_status or 
                            (current_avg_fill_price and current_avg_fill_price > 0 and auto_order.price != current_avg_fill_price)):
                            
                            # Update the auto_order with latest data from IBKR
                            auto_order.status = current_status
                            if current_avg_fill_price and current_avg_fill_price > 0:
                                auto_order.price = current_avg_fill_price
                            
                            # Set executed_at if order is filled and not already set
                            if current_status == "Filled" and auto_order.executed_at is None:
                                from datetime import datetime
                                auto_order.executed_at = now_local()
                                
                            
                                # strategy execution tracking removed; orders table records fills
                                pass

                                updated_orders = True
                                changed_orders.append((order_id_str, current_status))
                                ibkr_logger.info(f"Updated order {order_id_str}: status={current_status}, price={current_avg_fill_price}")
                            
                    else:
                        # 如果在auto_orders中找不到记录，可能是历史订单，默认为人工
                        detected_source_code = "manual"
                        order_source = "人工"
                else:
                    # 如果没有risk_manager，默认为人工
                    detected_source_code = "manual"
                    order_source = "人工"
                
                # 同步订单到 OrderManager
                if order_manager:
                    current_status = trade.orderStatus.status
                    current_avg_fill_price = trade.orderStatus.avgFillPrice
                    
                    if existing_order:
                        # 更新现有订单状态
                        if (existing_order.status != current_status or 
                            (current_avg_fill_price and current_avg_fill_price > 0 and existing_order.fill_price != current_avg_fill_price)):
                            
                            try:
                                from datetime import datetime
                                executed_at = None
                                if current_status == "Filled" and existing_order.executed_at is None:
                                    executed_at = now_local()
                                
                                # 从Trade对象的fills中提取commission和realizedPNL
                                commission = None
                                pnl = None
                                ibkr_logger.info(f"DEBUG: Processing order {order_id_str} for commission/PnL extraction")
                                ibkr_logger.info(f"DEBUG: Trade object type: {type(trade)}")
                                ibkr_logger.info(f"DEBUG: Trade object attributes: {dir(trade)}")
                                ibkr_logger.info(f"DEBUG: Trade has fills attribute: {hasattr(trade, 'fills')}")
                                
                                # 检查Trade对象的其他可能包含commission/pnl的属性
                                if hasattr(trade, 'orderStatus'):
                                    ibkr_logger.info(f"DEBUG: Trade.orderStatus: {trade.orderStatus}")
                                    ibkr_logger.info(f"DEBUG: OrderStatus attributes: {dir(trade.orderStatus)}")
                                if hasattr(trade, 'contract'):
                                    ibkr_logger.info(f"DEBUG: Trade.contract: {trade.contract}")
                                if hasattr(trade, 'order'):
                                    ibkr_logger.info(f"DEBUG: Trade.order: {trade.order}")
                                    ibkr_logger.info(f"DEBUG: Order attributes: {dir(trade.order)}")
                                
                                if hasattr(trade, 'fills'):
                                    ibkr_logger.info(f"DEBUG: Trade.fills value: {trade.fills}")
                                    ibkr_logger.info(f"DEBUG: Trade.fills type: {type(trade.fills)}")
                                    if trade.fills:
                                        total_commission = 0
                                        total_pnl = 0
                                        ibkr_logger.info(f"Processing {len(trade.fills)} fills for order {order_id_str}")
                                        for i, fill in enumerate(trade.fills):
                                            ibkr_logger.info(f"DEBUG: Fill {i}: {fill}")
                                            ibkr_logger.info(f"DEBUG: Fill {i} type: {type(fill)}")
                                            ibkr_logger.info(f"DEBUG: Fill {i} has commissionReport: {hasattr(fill, 'commissionReport')}")
                                            
                                            if hasattr(fill, 'commissionReport'):
                                                ibkr_logger.info(f"DEBUG: Fill {i} commissionReport: {fill.commissionReport}")
                                                if fill.commissionReport:
                                                    if hasattr(fill.commissionReport, 'commission'):
                                                        fill_commission = fill.commissionReport.commission
                                                        total_commission += fill_commission
                                                        ibkr_logger.info(f"Fill {i} commission: {fill_commission}, total: {total_commission}")
                                                    else:
                                                        ibkr_logger.info(f"DEBUG: Fill {i} commissionReport has no commission attribute")
                                                    
                                                    if hasattr(fill.commissionReport, 'realizedPNL'):
                                                        fill_pnl = fill.commissionReport.realizedPNL
                                                        total_pnl += fill_pnl
                                                        ibkr_logger.info(f"Fill {i} PnL: {fill_pnl}, total: {total_pnl}")
                                                    else:
                                                        ibkr_logger.info(f"DEBUG: Fill {i} commissionReport has no realizedPNL attribute")
                                                else:
                                                    ibkr_logger.info(f"DEBUG: Fill {i} commissionReport is None/empty")
                                            else:
                                                ibkr_logger.info(f"Fill {i} has no commissionReport attribute")
                                        
                                        commission = total_commission
                                        pnl = total_pnl
                                        ibkr_logger.info(f"Order {order_id_str} final commission: {commission}, PnL: {pnl}")
                                    else:
                                        ibkr_logger.info(f"DEBUG: Trade.fills is empty for order {order_id_str}")
                                else:
                                    ibkr_logger.info(f"DEBUG: Trade has no fills attribute for order {order_id_str}")
                                
                                order_manager.update_order_status(
                                    order_id=order_id_str,
                                    status=current_status,
                                    fill_price=current_avg_fill_price if current_avg_fill_price and current_avg_fill_price > 0 else None,
                                    executed_at=executed_at,
                                    commission=commission,
                                    pnl=pnl
                                )
                                ibkr_logger.debug(f"OrderManager: Updated order {order_id_str} status to {current_status}")
                                
                                # 检查是否需要自动创建风险规则（针对新资产的市价单）
                                if (current_status == "Filled" and current_avg_fill_price and current_avg_fill_price > 0 and 
                                    hasattr(order_manager, 'check_and_create_auto_risk_rule')):
                                    try:
                                        order_manager.check_and_create_auto_risk_rule(
                                            order_id=order_id_str,
                                            fill_price=current_avg_fill_price,
                                            ibkr_client=self,
                                            risk_manager=risk_manager
                                        )
                                    except Exception as risk_err:
                                        ibkr_logger.warning(f"Failed to create auto risk rule for order {order_id_str}: {risk_err}")
                                
                                # 不再从 IB 同步覆盖 order_source，避免覆盖用户/策略设置，仅更新状态/成交价/时间
                                ibkr_logger.debug(f"Skip syncing order_source for {order_id_str}; keep existing value: {existing_order.order_source}")
                            except Exception as om_err:
                                ibkr_logger.warning(f"Failed to update order {order_id_str} in OrderManager: {om_err}")
                    else:
                        # 若该订单此前被软删除，则跳过创建，防止从IBKR重新同步
                        try:
                            if hasattr(order_manager, 'was_order_deleted') and order_manager.was_order_deleted(order_id_str):
                                ibkr_logger.info(f"Skip re-creating soft-deleted order {order_id_str} from IBKR sync")
                            else:
                                # 为从 IBKR 获取的历史订单创建记录（如果不存在）
                                # 尝试根据 IBKR 的订单信息推断订单类型
                                order_type = "MKT" if trade.order.orderType.upper() in ('MKT', 'MARKET') else "LMT"
                                price = getattr(trade.order, 'lmtPrice', None) or current_avg_fill_price
                                
                                # 获取订单来源信息
                                detected_source = "manual"  # 默认为手动
                                if existing_order and existing_order.order_source:
                                    detected_source = existing_order.order_source
                                elif order_source == "机器人":
                                    detected_source = "auto"
                                
                                order_manager.create_order(
                                    order_id=order_id_str,
                                    symbol=getattr(trade.contract, 'localSymbol', None) or trade.contract.symbol,
                                    action=trade.order.action,
                                    quantity=trade.order.totalQuantity,
                                    order_type=order_type,
                                    price=price,
                                    order_source=detected_source,
                                    strategy_name=None,  # 暂无法从 IBKR 推断
                                    rule_id=None,
                                    parent_order_id=None,
                                    exchange=getattr(trade.contract, 'exchange', 'SMART'),
                                    sec_type=getattr(trade.contract, 'secType', 'STK'),
                                    notes=f"从 IBKR 同步的历史订单"
                                )
                                
                                # 立即更新状态和成交价
                                if current_status != "Submitted":
                                    from datetime import datetime
                                    executed_at = None
                                    if current_status == "Filled":
                                        executed_at = now_local()
                                    
                                    # 从Trade对象的fills中提取commission和realizedPNL
                                    commission = None
                                    pnl = None
                                    if hasattr(trade, 'fills') and trade.fills:
                                        total_commission = 0
                                        total_pnl = 0
                                        ibkr_logger.info(f"Processing {len(trade.fills)} fills for historical order {order_id_str}")
                                        for fill in trade.fills:
                                            if hasattr(fill, 'commissionReport') and fill.commissionReport:
                                                if hasattr(fill.commissionReport, 'commission'):
                                                    fill_commission = fill.commissionReport.commission
                                                    total_commission += fill_commission
                                                    ibkr_logger.info(f"Historical fill commission: {fill_commission}, total: {total_commission}")
                                                if hasattr(fill.commissionReport, 'realizedPNL'):
                                                    fill_pnl = fill.commissionReport.realizedPNL
                                                    total_pnl += fill_pnl
                                                    ibkr_logger.info(f"Historical fill PnL: {fill_pnl}, total: {total_pnl}")
                                            else:
                                                ibkr_logger.info(f"Historical fill has no commissionReport: {fill}")
                                        commission = total_commission
                                        pnl = total_pnl
                                        ibkr_logger.info(f"Historical order {order_id_str} final commission: {commission}, PnL: {pnl}")
                                    else:
                                        ibkr_logger.info(f"Historical order {order_id_str} has no fills or fills is empty")
                                    
                                    order_manager.update_order_status(
                                        order_id=order_id_str,
                                        status=current_status,
                                        fill_price=current_avg_fill_price if current_avg_fill_price and current_avg_fill_price > 0 else None,
                                        executed_at=executed_at,
                                        commission=commission,
                                        pnl=pnl
                                    )
                                
                                ibkr_logger.info(f"OrderManager: Created historical order {order_id_str} from IBKR sync")
                        except Exception as create_err:
                            ibkr_logger.warning(f"Failed to create historical order {order_id_str} in OrderManager: {create_err}")
                
                # Add debug logging for order data
                ibkr_logger.debug(f"Processing order {order_id_str}: totalQuantity={trade.order.totalQuantity}, action={trade.order.action}, orderType={trade.order.orderType}, status={trade.orderStatus.status}")
                
                orders.append({
                    'order_id': order_id_str,
                    'symbol': getattr(trade.contract, 'localSymbol', None) or trade.contract.symbol,
                    'action': trade.order.action,
                    'quantity': trade.order.totalQuantity,
                    'order_type': trade.order.orderType,
                    'limit_price': getattr(trade.order, 'lmtPrice', None),
                    'status': trade.orderStatus.status,
                    'filled': trade.orderStatus.filled,
                    'remaining': trade.orderStatus.remaining,
                    'avg_fill_price': trade.orderStatus.avgFillPrice,
                    'submit_time': trade.log[0].time if trade.log else None,
                    'order_source': order_source
                })
            
            # Save updated orders to database if any were changed
            if updated_orders and risk_manager:
                for oid, new_status in changed_orders:
                    try:
                        risk_manager.update_order_status(oid, new_status)
                    except Exception as e:
                        ibkr_logger.warning(f"Failed to persist updated order {oid}: {e}")
                ibkr_logger.info("Updated order data saved to database through RiskManager")
            
            return orders

        except Exception as e:
            ibkr_logger.error(f"Failed to get orders: {e}")
            return []

    async def get_account_info(self) -> Dict:
        """获取账户信息"""
        try:
            # 确保连接
            if not self.is_connected():
                ibkr_logger.error("Cannot retrieve account info: IBKR is not connected")
                return self._return_cached_account_info()

            # 获取IB实例
            ib = self.ib
            if not ib or not ib.isConnected():
                ibkr_logger.error(f"IBKR connection lost for get account info")
                return self._return_cached_account_info()

            # Use synchronous methods in async context
            account_values = ib.accountValues()

            # Start from the last cached snapshot when available so that we don't
            # accidentally wipe fields to zero if IBKR returns a partial update.
            info = self._return_cached_account_info() or {}
            info = dict(info)  # ensure we don't mutate the cached reference

            # Guarantee all expected keys exist even when we start from a cached
            # snapshot created by an earlier version of the code.
            info.setdefault('account_id', None)
            info['account_type'] = (
                'Paper' if connection_manager.config.paper_trading else 'Live'
            )
            info.setdefault('total_cash', 0.0)
            info.setdefault('available_funds', 0.0)
            info.setdefault('buying_power', 0.0)
            info.setdefault('net_liquidation', 0.0)
            info.setdefault('unrealized_pnl', 0.0)
            info.setdefault('realized_pnl', 0.0)
            
            # Get account ID from account values
            def _parse_float(val: Any) -> float:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    return 0.0

            for item in account_values:
                try:
                    currency = (item.currency or '').upper()
                except AttributeError:
                    currency = ''

                # Account id may be stored either in AccountCode value or the account field itself
                if item.tag == 'AccountCode' and item.value:
                    info['account_id'] = item.value
                elif not info['account_id'] and getattr(item, 'account', None):
                    info['account_id'] = item.account

                # IBKR may report base currency values using either USD or BASE depending on
                # account configuration, so accept both to avoid zeroing out valid data.
                currency_ok = currency in ('USD', 'BASE', '')

                if item.tag == 'TotalCashValue' and currency_ok:
                    info['total_cash'] = _parse_float(item.value)
                elif item.tag == 'AvailableFunds' and currency_ok:
                    info['available_funds'] = _parse_float(item.value)
                elif item.tag == 'BuyingPower' and currency_ok:
                    info['buying_power'] = _parse_float(item.value)
                elif item.tag == 'NetLiquidation' and currency_ok:
                    info['net_liquidation'] = _parse_float(item.value)
                elif item.tag == 'UnrealizedPnL' and currency_ok:
                    info['unrealized_pnl'] = _parse_float(item.value)
                elif item.tag == 'RealizedPnL' and currency_ok:
                    info['realized_pnl'] = _parse_float(item.value)

            # Persist snapshot for downstream consumers (risk manager, summaries)
            try:
                self._cached_account_info = dict(info)
                self._cached_account_info_timestamp = now_local()
            except Exception:
                pass

            return info
        except Exception as e:
            ibkr_logger.error(f"Failed to get account info: {e}")
            return self._return_cached_account_info()
    
    async def get_account_summary(self) -> List[Dict]:
        """兼容方法：返回与 ib.accountSummary() 类似的结构，用于资金充足性校验等逻辑。
        返回格式：[{'tag': str, 'value': Any, 'currency': str|None, 'account': str|None}, ...]
        """
        try:
            # 确保连接
            if not self.is_connected():
                ibkr_logger.error("Cannot retrieve account summary: IBKR is not connected")
                return []

            ib = self.ib
            if not ib or not ib.isConnected():
                ibkr_logger.error("IBKR connection lost for get_account_summary")
                return []

            summary_items = ib.accountSummary()
            result: List[Dict] = []
            for it in summary_items or []:
                try:
                    result.append({
                        'tag': getattr(it, 'tag', None),
                        'value': getattr(it, 'value', None),
                        'currency': getattr(it, 'currency', None),
                        'account': getattr(it, 'account', None),
                    })
                except Exception:
                    # 忽略单条解析错误，继续收集
                    continue
            return result
        except Exception as e:
            ibkr_logger.error(f"Failed to get account summary: {e}")
            return []

    async def get_positions(self, fast: bool = False) -> List[Dict]:
        """获取持仓列表
        fast=False 时会尝试获取实时价格，确保价格数据最新
        fast=True 时仅使用缓存或 portfolio 的 marketPrice/averageCost，跳过逐个实时行情请求以显著降低延迟"""
        try:
            # 确保连接
            if not self.is_connected():
                ibkr_logger.error("Cannot retrieve positions: IBKR is not connected")
                return []
            
            # 获取IB实例
            ib = self.ib
            if not ib or not ib.isConnected():
                ibkr_logger.error(f"IBKR connection lost for get positions")
                return []
            
            # Use portfolio() to get current market data snapshot without blocking the event loop
            def _portfolio_snapshot():
                return ib.portfolio()

            portfolio_items = await asyncio.to_thread(
                self.connection_manager.run_in_connection_thread,
                _portfolio_snapshot,
                timeout=10.0,
            )
            if portfolio_items is None:
                portfolio_items = []

            # 当发生短暂断线/同步延迟时，IB可能返回空的portfolio快照。
            # 为避免前端仓位瞬时清空，这里使用短期缓存回退。
            if not portfolio_items:
                if getattr(self, 'positions_cache', None) and getattr(self, 'positions_cache_timestamp', None) and (now_local() - self.positions_cache_timestamp) < timedelta(seconds=self.positions_cache_ttl_seconds):
                    ibkr_logger.warning("IB portfolio() returned empty; using recent positions cache to avoid flicker")
                    return self.positions_cache.copy()
                else:
                    ibkr_logger.warning("IB portfolio() returned empty and no valid cache; returning empty list")
                    return []
            position_list = []

            realtime_tasks: Dict[str, asyncio.Task] = {}
            realtime_results: Dict[str, Optional[Dict[str, Any]]] = {}
            if not fast:
                fetch_semaphore = asyncio.Semaphore(5)

                async def _prefetch(symbol: str):
                    async with fetch_semaphore:
                        return await self.get_real_time_data(symbol)

                for item in portfolio_items:
                    if not getattr(item, 'position', 0):
                        continue
                    sym = getattr(getattr(item, 'contract', None), 'symbol', None)
                    if not sym or sym in realtime_tasks:
                        continue
                    cache_ts = self.price_cache_timestamp.get(sym)
                    cached_price = self.price_cache.get(sym)
                    if cached_price and cache_ts and (now_local() - cache_ts) < timedelta(seconds=self.price_cache_ttl_seconds):
                        continue
                    realtime_tasks[sym] = asyncio.create_task(_prefetch(sym))

            for item in portfolio_items:
                # Filter out zero positions to avoid stale data
                if item.position == 0:
                    continue

                sym = item.contract.symbol
                current_price = None
                price_source = None

                # 合约乘数用于期货/期权的名义价值和PnL计算
                contract_multiplier = 1.0
                raw_multiplier = getattr(item.contract, "multiplier", None)
                if raw_multiplier is not None:
                    try:
                        contract_multiplier = float(raw_multiplier) or 1.0
                    except (TypeError, ValueError):
                        contract_multiplier = 1.0

                # 额外的乘数映射，确保常见期货合约的乘数正确
                multiplier_overrides = {
                    "MNQ": 2.0,   # Micro E-mini Nasdaq-100
                    "NQ": 20.0,   # E-mini Nasdaq-100
                    "MES": 5.0,
                    "ES": 50.0,
                    "MYM": 0.5,
                    "YM": 5.0,
                    "M2K": 5.0,
                    "RTY": 50.0,
                    "MCL": 100.0,
                    "CL": 1000.0,
                    "MGC": 10.0,
                    "GC": 100.0,
                }
                if sym in multiplier_overrides:
                    contract_multiplier = multiplier_overrides[sym]
                if not math.isfinite(contract_multiplier) or contract_multiplier <= 0:
                    contract_multiplier = 1.0
                
                # 1) 首先检查缓存是否有效
                ts = self.price_cache_timestamp.get(sym)
                if ts and now_local() - ts < timedelta(seconds=self.price_cache_ttl_seconds):
                    cached_price = self.price_cache.get(sym)
                    if cached_price and cached_price > 0:
                        current_price = cached_price
                        price_source = "cache"
                        ibkr_logger.debug(f"Using cached price for {sym}: {current_price}")
                
                # 2) 如果缓存无效，尝试获取实时价格（仅在非快速模式下且不在ThreadPoolExecutor中）
                if current_price is None and not fast:
                    if sym in realtime_results:
                        data = realtime_results[sym]
                    else:
                        task = realtime_tasks.get(sym)
                        if task:
                            try:
                                data = await asyncio.wait_for(task, timeout=4.0)
                            except asyncio.TimeoutError:
                                task.cancel()
                                ibkr_logger.warning(f"Realtime price request for {sym} timed out")
                                data = None
                            except asyncio.CancelledError:
                                ibkr_logger.debug(f"Realtime price task for {sym} cancelled")
                                raise
                            except Exception as e:
                                ibkr_logger.debug(f"Realtime price fetch error for {sym}: {e}")
                                data = None
                            else:
                                realtime_results[sym] = data
                            finally:
                                realtime_tasks.pop(sym, None)
                        else:
                            data = None
                        if sym not in realtime_results:
                            realtime_results[sym] = data
                    if data and data.get('last'):
                        current_price = data['last']
                        price_source = "realtime"

                # 3) 如果实时获取失败或为快速模式，尝试使用 marketPrice
                if current_price is None:
                    if item.marketPrice and item.marketPrice == item.marketPrice and item.marketPrice > 0:
                        current_price = item.marketPrice
                        price_source = "marketPrice"
                        ibkr_logger.debug(f"Using marketPrice for {sym}: {current_price}")
                
                # 4) 最后兜底 averageCost
                if current_price is None:
                    if item.averageCost and item.averageCost > 0:
                        current_price = item.averageCost
                        price_source = "averageCost"
                        ibkr_logger.debug(f"Using averageCost as fallback for {sym}: {current_price}")
                    else:
                        current_price = 0
                        price_source = "unknown"
                        ibkr_logger.warning(f"No valid price source found for {sym}")
                
                ibkr_logger.debug(f"Portfolio data for {sym}: marketPrice={item.marketPrice}, averageCost={item.averageCost}, position={item.position}")
                ibkr_logger.debug(f"Price for {sym}: {current_price} (source: {price_source})")
                
                position_qty = float(item.position or 0)

                # IBKR 对期货返回的 averageCost 已经乘以乘数，因此除以乘数得到每份合约的成本价
                avg_cost_raw = float(item.averageCost or 0.0)
                if contract_multiplier != 0:
                    avg_cost_adjusted = avg_cost_raw / contract_multiplier
                else:
                    avg_cost_adjusted = avg_cost_raw

                if not math.isfinite(avg_cost_adjusted):
                    avg_cost_adjusted = 0.0

                market_value = position_qty * (current_price or 0) * contract_multiplier

                # 计算未实现盈亏，统一按照乘数处理（避免依赖可能不准确的IB字段）
                current_price_value = current_price or 0.0
                unrealized_pnl = (current_price_value - avg_cost_adjusted) * position_qty * contract_multiplier

                # 如果我们缺少价格，尝试回退到 IBKR 提供的未实现盈亏
                ib_unrealized = getattr(item, 'unrealizedPNL', None)
                try:
                    ib_unrealized_val = float(ib_unrealized)
                except (TypeError, ValueError):
                    ib_unrealized_val = None

                if (current_price_value == 0 or not math.isfinite(unrealized_pnl)) and ib_unrealized_val is not None:
                    unrealized_pnl = ib_unrealized_val

                if not math.isfinite(unrealized_pnl):
                    unrealized_pnl = 0.0

                # 计算未实现盈亏百分比（以持仓名义价值为基准）
                position_notional = abs(position_qty * avg_cost_adjusted * contract_multiplier)
                if position_notional:
                    unrealized_pnl_percent = (unrealized_pnl / position_notional) * 100
                else:
                    unrealized_pnl_percent = 0.0
                
                position_info = {
                    'symbol': sym,
                    'position': item.position,
                    'avg_cost': avg_cost_adjusted,
                    'market_price': item.marketPrice if item.marketPrice == item.marketPrice else 0,
                    'current_price': current_price,
                    'market_value': market_value,
                    'contract_multiplier': contract_multiplier,
                    'unrealized_pnl': unrealized_pnl,
                    'unrealized_pnl_percent': unrealized_pnl_percent,
                    'price_source': price_source,
                }
                position_list.append(position_info)
            
            # 成功获取非空持仓后，刷新短期缓存
            if position_list:
                self.positions_cache = position_list.copy()
                self.positions_cache_timestamp = now_local()
            
            return position_list
        except Exception as e:
            ibkr_logger.error(f"Failed to get positions: {e}")
            return []

    async def _handle_order_error(self, req_id, error_code, error_string):
        """处理订单错误，更新订单状态为失败"""
        try:
            from .order_manager import get_order_manager, OrderStatus
            
            # 记录错误信息
            connection_logger.error(f"Order error - ReqId: {req_id}, Code: {error_code}, Message: {error_string}")
            
            # 从IBKRClient获取reqId到订单ID的映射
            order_id = self.req_id_to_order_id.get(req_id)
            if order_id:
                # 更新订单状态为失败
                order_manager = get_order_manager()
                await asyncio.to_thread(
                    order_manager.update_order_status,
                    order_id=order_id,
                    status=OrderStatus.REJECTED.value,
                    notes=f"IBKR Error {error_code}: {error_string}"
                )
                connection_logger.info(f"Updated order {order_id} status to REJECTED due to error {error_code}")
                
                # 清理映射
                del self.req_id_to_order_id[req_id]
            else:
                connection_logger.warning(f"No order mapping found for reqId {req_id}")
            
        except Exception as e:
            connection_logger.error(f"Failed to handle order error: {e}")
    
    async def _handle_order_filled(self, trade):
        """处理订单成交事件"""
        try:
            order_id = str(trade.order.orderId)
            connection_logger.info(f"Handling filled order: {order_id}")
            
            # 获取佣金和PnL信息
            total_commission = 0
            total_pnl = 0
            
            for fill in trade.fills:
                if hasattr(fill, 'commissionReport') and fill.commissionReport:
                    total_commission += fill.commissionReport.commission
                    if hasattr(fill.commissionReport, 'realizedPNL'):
                        total_pnl += fill.commissionReport.realizedPNL or 0
            
            connection_logger.info(f"Order {order_id} commission: {total_commission}, pnl: {total_pnl}")
            
            # 更新数据库中的订单信息
            await self._update_order_commission_pnl(order_id, total_commission, total_pnl)
            
        except Exception as e:
            connection_logger.error(f"Error handling filled order: {e}")
    
    async def _update_order_commission_pnl(self, order_id, commission, pnl):
        """更新订单的佣金和PnL到数据库并同步内存对象"""
        try:
            from .database import get_db_manager
            
            db = get_db_manager()
            oid = str(order_id)
            await db.update_order_commission_pnl(oid, commission, pnl)
            connection_logger.info(f"Updated order {oid} commission={commission}, pnl={pnl} in database")

            # 同步内存中的订单（如果存在）
            try:
                from .order_manager import get_order_manager
                om = get_order_manager()
                oid = str(order_id)
                order = om.orders.get(oid)
                if order:
                    prev_comm, prev_pnl = order.commission, order.pnl
                    if commission is not None:
                        order.commission = commission
                    if pnl is not None:
                        order.pnl = pnl
                    connection_logger.debug(
                        f"Synchronized in-memory order {oid} commission: {prev_comm} -> {order.commission}, pnl: {prev_pnl} -> {order.pnl}"
                    )
                    try:
                        if getattr(order, "status", "").lower() == "filled":
                            from .utils.strategy_cache import (
                                record_filled_order as cache_record_filled_order,
                            )
                            from .utils.account_pnl_cache import (
                                record_account_fill as cache_record_account_fill,
                            )

                            await cache_record_filled_order(order)
                            await cache_record_account_fill(order)
                    except Exception as cache_err:
                        connection_logger.debug(
                            f"Strategy cache update skipped for order {oid}: {cache_err}"
                        )
                else:
                    connection_logger.debug(f"In-memory order {oid} not found for commission/pnl sync")
            except Exception as sync_err:
                connection_logger.debug(f"Failed to sync in-memory order commission/pnl: {sync_err}")
            
            # 将最新的佣金和PnL更新通过 WebSocket 广播给前端，触发订单列表刷新
            try:
                from .websocket_manager import websocket_manager
                message = {
                    "type": "order_update",
                    "order_id": oid,
                    "commission": commission,
                    "pnl": pnl,
                }
                # 如果拿到了内存中的订单对象，补充更多字段（与 order_filled 事件保持一致的结构）
                if 'order' in locals() and order:
                    message.update({
                        "symbol": getattr(order, 'symbol', None),
                        "action": getattr(order, 'action', None),
                        "quantity": getattr(order, 'quantity', None),
                        "fill_price": getattr(order, 'fill_price', None),
                        "order_type": getattr(order, 'order_type', None),
                        "executed_at": order.executed_at.isoformat() if getattr(order, 'executed_at', None) else None,
                    })
                await websocket_manager.broadcast(message)
                connection_logger.info(f"Broadcasted order_update for order {oid} (commission={commission}, pnl={pnl})")
            except Exception as ws_err:
                connection_logger.warning(f"Failed to broadcast order_update for order {oid}: {ws_err}")
            
        except Exception as e:
            connection_logger.error(f"Error updating order commission/pnl in database: {e}")
    
    async def backfill_commissions(self, days: int = 7) -> int:
        """Backfill commission and PnL for recent executions by requesting executions from IBKR.
        This will not override existing non-null commission/pnl in DB thanks to COALESCE guards.
        Returns the number of update attempts made.
        """
        try:
            if not self.ib or not self.ib.isConnected():
                connection_logger.error("Cannot backfill commissions: IB not connected")
                return 0

            async def _backfill_op():
                try:
                    ib_local = self.ib
                    # Compute start time for the execution filter (local time, format YYYYMMDD-HH:MM:SS)
                    start_dt = now_local() - timedelta(days=max(1, int(days)))
                    # IB API expects a hyphen between date and time, e.g. 20250131-13:45:00
                    start_str = start_dt.strftime('%Y%m%d-%H:%M:%S')

                    # Request executions; this will also trigger commissionReport events
                    filt = ExecutionFilter(time=start_str)
                    connection_logger.info(f"Requesting executions since {start_str} for backfilling commissions/PnL")
                    try:
                        await ib_local.reqExecutionsAsync(filt)
                    except Exception as e:
                        connection_logger.warning(f"reqExecutionsAsync failed: {e}; trying sync fallback")
                        try:
                            ib_local.reqExecutions(filt)
                        except Exception as e2:
                            connection_logger.error(f"Sync reqExecutions failed: {e2}")
                            return 0

                    # Allow some time for commission reports to arrive and pair with executions
                    await asyncio.sleep(1.5)

                    fills = ib_local.fills()
                    if not fills:
                        connection_logger.info("No fills returned from IBKR for the specified window")
                        return 0

                    update_count = 0
                    for fill in fills:
                        try:
                            # Prefer commissionReport attached to fill
                            cr = getattr(fill, 'commissionReport', None)
                            exec_obj = getattr(fill, 'execution', None)
                            if cr and exec_obj and hasattr(exec_obj, 'orderId'):
                                order_id = str(exec_obj.orderId)
                                commission = cr.commission
                                pnl = getattr(cr, 'realizedPNL', None)
                                connection_logger.debug(
                                    f"Backfill commission report for order {order_id}: commission={commission}, pnl={pnl}"
                                )
                                # Use the same updater which syncs DB and in-memory order
                                await self._update_order_commission_pnl(order_id, commission, pnl)
                                update_count += 1
                        except Exception as inner_e:
                            connection_logger.warning(f"Failed to process a fill during backfill: {inner_e}")

                    connection_logger.info(f"Backfill completed. Updates attempted: {update_count}")
                    return update_count
                except Exception as e:
                    connection_logger.error(f"Backfill operation failed: {e}")
                    return 0

            result = self.run_async_in_connection_thread(_backfill_op())
            try:
                return int(result or 0)
            except Exception:
                return 0
        except Exception as e:
            connection_logger.error(f"Backfill commissions wrapper error: {e}")
            return 0

if __name__ == "__main__":
    """主函数，用于调试下单方法"""
    print("Starting IBKR Client Debug...")
    
    # 创建客户端实例
    client = IBKRClient()
    
    try:
        # 自动连接到IB
        print("Connecting to IBKR...")
        if client.connect():
            print("Successfully connected to IBKR")
            
            # 获取账户信息
            print("\nGetting account info...")
            account_info = client.get_account_info()
            print(f"Account ID: {account_info.get('account_id')}")
            print(f"Account Type: {account_info.get('account_type')}")
            print(f"Available Funds: ${account_info.get('available_funds', 0):,.2f}")
            print(f"Buying Power: ${account_info.get('buying_power', 0):,.2f}")
            
            # 获取当前持仓
            print("\nGetting positions...")
            # 需要await异步方法
            import asyncio
            positions = asyncio.run(client.get_positions())
            if positions:
                for pos in positions:
                    print(f"Position: {pos['symbol']} - {pos['position']} shares @ ${pos['avg_cost']:.2f}")
            else:
                print("No positions found")
            
            # 获取当前订单
            print("\nGetting orders...")
            orders = client.get_orders()
            if orders:
                for order in orders:
                    print(f"Order: {order['order_id']} - {order['action']} {order['quantity']} {order['symbol']} @ {order.get('limit_price', 'Market')} - Status: {order['status']}")
            else:
                print("No active orders found")
            
            # 测试下单功能（注释掉以避免意外下单）
            print("\n=== Order Testing (Commented Out) ===")
            print("# Test market order:")
            print("# trade = client.place_market_order('AAPL', 1, 'BUY')")
            # trade = client.place_market_order('MNQ', 1, 'BUY')
            print("# if trade:")
            print("#     print(f'Market order placed: {trade.order.orderId}')")
            print("# else:")
            print("#     print('Failed to place market order')")
            
            print("\n# Test limit order:")
            print("# trade = client.place_limit_order('AAPL', 1, 150.0, 'BUY')")
            trade = client.place_limit_order('MNQ', 1, 23360, 'BUY')
            print("# if trade:")
            print("#     print(f'Limit order placed: {trade.order.orderId}')")
            print("# else:")
            print("#     print('Failed to place limit order')")
            
            print("\n# Test future order (MNQ):")
            print("# trade = client.place_market_order('MNQ', 1, 'BUY', sec_type='FUT')")
            print("# if trade:")
            print("#     print(f'Future order placed: {trade.order.orderId}')")
            print("# else:")
            print("#     print('Failed to place future order')")
            
        else:
            print("Failed to connect to IBKR")
            
    except Exception as e:
        print(f"Error during debug: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        # 断开连接
        print("\nDisconnecting...")
        client.disconnect()
        print("Debug session completed")
