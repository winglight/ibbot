from __future__ import annotations

import asyncio
import importlib
import concurrent.futures
import copy
import csv
import itertools
import math
from contextlib import suppress
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ib_insync import IB, LimitOrder, MarketOrder, Order  # type: ignore
else:
    try:  # pragma: no cover - optional dependency
        from ib_insync import IB, LimitOrder, MarketOrder, Order  # type: ignore
    except ImportError:  # pragma: no cover - fallback when ib_insync is absent
        IB = None  # type: ignore[assignment]
        LimitOrder = MarketOrder = Order = None  # type: ignore[assignment]

try:  # pragma: no cover - exercised indirectly in CI when ibapi is unavailable
    from ibapi.contract import Contract as IBAPIContract, ContractDetails as IBAPIContractDetails
    from ibapi.order import Order as IBAPIOrder, OrderState as IBAPIOrderState
except ImportError:  # pragma: no cover - fallback used during unit tests
    from test.ibapi_stub import (  # type: ignore
        Contract as IBAPIContract,
        ContractDetails as IBAPIContractDetails,
        Order as IBAPIOrder,
        OrderState as IBAPIOrderState,
    )
from loguru import logger
from zoneinfo import ZoneInfo

from src.data_layer import (
    DataSubscriptionRequest,
    DataSubscriptionToken,
    DataSourceError,
    get_data_layer_mode,
    get_data_source_manager,
    is_dual_mode,
    is_legacy_mode,
)
from src.routes.service_clients import (
    MarketDataServiceClient,
    OrdersServiceClient,
    ServiceClientError,
    ServiceResponseError,
    ServiceUnavailableError,
)
from src.connection_manager import ConnectionManager, connection_manager as _GLOBAL_CONNECTION_MANAGER
from src.ibapi_controller import ConnectionParams, IBAPIController, IBAPIRequestError, OrderPlacement
from src.market_data_router import MarketDataRouter
from src.utils import is_trading_disabled, now_local, to_local_time
from src.utils.ibkr_serialization import trade_to_dict

# Module-specific logger for ibkr_client
ibkr_logger = logger.bind(module="ibkr_client", request_id="-")

if TYPE_CHECKING:  # pragma: no cover - typing only
    import pandas as pd  # type: ignore
else:
    _pandas_env = (os.getenv("IBBOT_ENABLE_PANDAS") or "auto").strip().lower()
    _PANDAS_AVAILABLE = False
    _PANDAS_REASON: str | None = None

    if _pandas_env in {"0", "false", "no", "off"}:
        _PANDAS_REASON = "disabled via IBBOT_ENABLE_PANDAS"
    else:
        try:
            probe = subprocess.run(
                [sys.executable, "-c", "import pandas"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5,
                check=False,
            )
        except Exception as exc:  # pragma: no cover - probe failures fall back to stub
            _PANDAS_REASON = f"probe failure: {exc}"
        else:
            if probe.returncode == 0:
                try:
                    import pandas as pd  # type: ignore  # pragma: no cover - heavy dependency
                except Exception as exc:  # pragma: no cover - defensive guard
                    _PANDAS_REASON = str(exc)
                else:
                    _PANDAS_AVAILABLE = True
                    _PANDAS_REASON = None
            else:
                stderr = probe.stderr.decode(errors="ignore").strip()
                _PANDAS_REASON = stderr or f"exit code {probe.returncode}"

    if not _PANDAS_AVAILABLE:
        class _PandasStub:
            """Raising proxy used when pandas cannot be imported."""

            def __init__(self, reason: str | None) -> None:
                self._reason = reason or "pandas unavailable"

            def __getattr__(self, name: str) -> Any:  # pragma: no cover - trivial
                raise RuntimeError(
                    "pandas support unavailable ({}); set IBBOT_ENABLE_PANDAS=1 and "
                    "ensure pandas is installed if this functionality is required.".format(self._reason)
                )

        pd = _PandasStub(_PANDAS_REASON)

_MARKET_DATA_SERVICE_CLIENT: Optional[MarketDataServiceClient] = None
_ORDERS_SERVICE_CLIENT: Optional[OrdersServiceClient] = None
_MARKET_DATA_INGEST_PATH = "/market/historical/ingest"


class IBKRConnectionError(RuntimeError):
    """Raised when the IBKR client cannot establish a connection on demand."""


def _get_market_data_service_client() -> MarketDataServiceClient:
    global _MARKET_DATA_SERVICE_CLIENT
    if _MARKET_DATA_SERVICE_CLIENT is None:
        _MARKET_DATA_SERVICE_CLIENT = MarketDataServiceClient()
    return _MARKET_DATA_SERVICE_CLIENT


def _get_orders_service_client() -> OrdersServiceClient:
    global _ORDERS_SERVICE_CLIENT
    if _ORDERS_SERVICE_CLIENT is None:
        _ORDERS_SERVICE_CLIENT = OrdersServiceClient()
    return _ORDERS_SERVICE_CLIENT

NY_TZ = ZoneInfo("America/New_York")


_CONTRACT_MONTH_CODE_MAP = {
    "F": 1,
    "G": 2,
    "H": 3,
    "J": 4,
    "K": 5,
    "M": 6,
    "N": 7,
    "Q": 8,
    "U": 9,
    "V": 10,
    "X": 11,
    "Z": 12,
}

_ALL_CONTRACT_MONTHS: Tuple[int, ...] = tuple(range(1, 13))

INDEX_FUTURE_DETAILS: Dict[str, Dict[str, Any]] = {
    "ES": {"exchange": "CME", "multiplier": "50", "tradingClass": "ES"},
    "MES": {"exchange": "CME", "multiplier": "5", "tradingClass": "MES"},
    "NQ": {"exchange": "CME", "multiplier": "20", "tradingClass": "NQ"},
    "MNQ": {"exchange": "CME", "multiplier": "2", "tradingClass": "MNQ"},
    "YM": {"exchange": "ECBOT", "multiplier": "5", "tradingClass": "YM"},
    "MYM": {"exchange": "ECBOT", "multiplier": "0.5", "tradingClass": "MYM"},
    "RTY": {"exchange": "CME", "multiplier": "50", "tradingClass": "RTY"},
    "M2K": {
        "exchange": "CME",
        "multiplier": "5",
        "tradingClass": "M2K",
        "contract_months": _ALL_CONTRACT_MONTHS,
    },
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


def _coerce_contract_months(value: Any) -> Tuple[int, ...]:
    """Normalise a contract-month definition into a tuple of integers."""

    if value is None:
        return _QUARTERLY_CONTRACT_MONTHS

    months: set[int] = set()

    if isinstance(value, (list, tuple, set, frozenset)):
        items = value
    else:
        items = (value,)

    for item in items:
        if item is None:
            continue

        if isinstance(item, int):
            candidate = item
            if 1 <= candidate <= 12:
                months.add(candidate)
            continue

        text = str(item).strip()
        if not text:
            continue

        if text.isdigit():
            candidate = int(text)
            if 1 <= candidate <= 12:
                months.add(candidate)
            continue

        for char in text:
            upper = char.upper()
            candidate = _CONTRACT_MONTH_CODE_MAP.get(upper)
            if candidate:
                months.add(candidate)

    if not months:
        return _QUARTERLY_CONTRACT_MONTHS

    return tuple(sorted(months))


def _front_contract(now: datetime, contract_months: Sequence[int] | None) -> Tuple[int, int]:
    """Determine the nearest active contract (year, month) for the given cycle."""

    months = _coerce_contract_months(contract_months)

    ny_now = now.astimezone(NY_TZ)
    year = ny_now.year
    month = ny_now.month

    for year_offset in range(0, 3):
        candidate_year = year + year_offset
        for candidate_month in months:
            if year_offset == 0 and candidate_month < month:
                continue

            expiry = _third_friday_datetime(candidate_year, candidate_month)
            if ny_now < expiry:
                return candidate_year, candidate_month

    # Fallback: if everything else failed, return the first month of the next cycle.
    return year + 1, months[0]


def _front_quarter_contract(now: datetime) -> Tuple[int, int]:
    """Determine the nearest active quarterly contract (year, month)."""

    return _front_contract(now, _QUARTERLY_CONTRACT_MONTHS)

@dataclass(slots=True)
class _MarketDataSubscription:
    """Track live market data subscriptions for remote services."""

    symbol: str
    contract: Any
    price_ticker: Any
    dom_ticker: Any | None
    price_handler: Callable[..., None]
    dom_handler: Callable[..., None] | None
    options: Dict[str, Any]
    dom_rows: int


_FINAL_ORDER_STATES = {"FILLED", "CANCELLED", "INACTIVE"}


@dataclass(slots=True)
class _OrderSnapshot:
    order_id: int
    contract: Any
    order: Any
    state: Any
    status: str
    filled: float
    remaining: float
    avg_fill_price: float
    last_fill_price: float
    perm_id: Optional[int]
    parent_id: Optional[int]
    client_id: Optional[int]
    why_held: Optional[str]
    mkt_cap_price: Optional[float]
    submitted_at: Optional[datetime] = None
    last_update: Optional[datetime] = None
    fills: List[Any] = field(default_factory=list)
    commission: Optional[float] = None
    realized_pnl: Optional[float] = None


class _TradeBook:
    """Maintain an in-memory view of live orders using ibapi events."""

    def __init__(
        self,
        *,
        loop_getter: Callable[[], Optional[asyncio.AbstractEventLoop]],
        commission_callback: Optional[Callable[[str, float, float], Awaitable[None]]],
    ) -> None:
        self._loop_getter = loop_getter
        self._commission_callback = commission_callback
        self._controller: Optional[IBAPIController] = None
        self._queue: Optional[asyncio.Queue[dict[str, Any]]] = None
        self._event_task: Optional[asyncio.Task[None]] = None
        self._orders: Dict[int, _OrderSnapshot] = {}
        self._exec_to_order: Dict[str, int] = {}
        self._exec_to_fill: Dict[str, Any] = {}
        self._lock = threading.RLock()

    async def attach(self, controller: IBAPIController) -> None:
        if controller is None:
            return

        if controller is self._controller and self._event_task and not self._event_task.done():
            return

        await self.detach()

        if not controller.is_connected():
            self._controller = controller
            return

        self._controller = controller
        loop = asyncio.get_running_loop()
        queue = controller.register_event_consumer()
        self._queue = queue
        self._event_task = loop.create_task(
            self._event_dispatch_loop(controller, queue),
            name="ibapi-trade-book",
        )

        try:
            await controller.request_open_orders_snapshot()
        except Exception:
            # Snapshot requests are best-effort; errors are logged by the caller.
            pass

    async def detach(self) -> None:
        task = self._event_task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        self._event_task = None
        self._queue = None
        self._controller = None
        with self._lock:
            self._orders.clear()
            self._exec_to_order.clear()
            self._exec_to_fill.clear()

    async def refresh(self) -> None:
        controller = self._controller
        if controller is None or not controller.is_connected():
            return
        try:
            await controller.request_open_orders_snapshot()
        except Exception:
            pass

    def record_placement(self, placement: OrderPlacement) -> None:
        with self._lock:
            snapshot = self._ensure_snapshot(placement.order_id)
            snapshot.contract = placement.contract
            snapshot.order = placement.order
            snapshot.state = placement.state
            status = getattr(placement.state, "status", None) if placement.state is not None else None
            if status:
                snapshot.status = str(status)
            snapshot.filled = self._coerce_float(getattr(placement.state, "filled", snapshot.filled))
            snapshot.remaining = self._coerce_float(getattr(placement.state, "remaining", snapshot.remaining))
            snapshot.avg_fill_price = self._coerce_float(
                getattr(placement.state, "avgFillPrice", snapshot.avg_fill_price)
            )
            snapshot.last_update = now_local()
            if snapshot.submitted_at is None:
                snapshot.submitted_at = snapshot.last_update

    def snapshot(self) -> List[_OrderSnapshot]:
        with self._lock:
            return [copy.copy(record) for record in self._orders.values()]

    def get(self, order_id: int) -> Optional[_OrderSnapshot]:
        with self._lock:
            record = self._orders.get(order_id)
            return copy.copy(record) if record else None

    def cancellable_order_ids(self) -> List[int]:
        with self._lock:
            return [
                order_id
                for order_id, record in self._orders.items()
                if record.status.upper() not in _FINAL_ORDER_STATES
            ]

    def _event_timestamp(self) -> datetime:
        return now_local()

    def mark_error(self, order_id: int, message: str) -> None:
        with self._lock:
            snapshot = self._orders.get(order_id)
            if snapshot is None:
                return
            snapshot.status = message
            snapshot.last_update = self._event_timestamp()

    async def _event_dispatch_loop(
        self,
        controller: IBAPIController,
        queue: asyncio.Queue[dict[str, Any]],
    ) -> None:
        try:
            while True:
                try:
                    event = await queue.get()
                except asyncio.CancelledError:
                    break
                except RuntimeError as exc:
                    if "Event loop is closed" in str(exc):
                        break
                    raise
                self._handle_event(event)
        finally:
            controller.unregister_event_consumer(queue)

    def _handle_event(self, event: Mapping[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "openOrder":
            self._handle_open_order(event)
        elif event_type == "orderStatus":
            self._handle_order_status(event)
        elif event_type == "execDetails":
            self._handle_execution(event)
        elif event_type == "commissionReport":
            self._handle_commission(event)
        elif event_type == "connectionClosed":
            with self._lock:
                self._orders.clear()
                self._exec_to_order.clear()
                self._exec_to_fill.clear()

    def _handle_open_order(self, event: Mapping[str, Any]) -> None:
        order_id = self._coerce_int(event.get("order_id"))
        if order_id is None or order_id <= 0:
            return

        contract = event.get("contract")
        order = event.get("order")
        state = event.get("order_state")

        now = self._event_timestamp()
        with self._lock:
            snapshot = self._ensure_snapshot(order_id)
            snapshot.contract = contract
            snapshot.order = order
            snapshot.state = state
            if state is not None:
                status = getattr(state, "status", None)
                if status:
                    snapshot.status = str(status)
                snapshot.filled = self._coerce_float(getattr(state, "filled", snapshot.filled))
                snapshot.remaining = self._coerce_float(getattr(state, "remaining", snapshot.remaining))
                snapshot.avg_fill_price = self._coerce_float(getattr(state, "avgFillPrice", snapshot.avg_fill_price))
                snapshot.last_fill_price = self._coerce_float(
                    getattr(state, "lastFillPrice", snapshot.last_fill_price)
                )
                snapshot.perm_id = self._coerce_int(getattr(state, "permId", snapshot.perm_id))
                snapshot.parent_id = self._coerce_int(getattr(state, "parentId", snapshot.parent_id))
                snapshot.client_id = self._coerce_int(getattr(state, "clientId", snapshot.client_id))
                snapshot.why_held = getattr(state, "whyHeld", snapshot.why_held)
                snapshot.mkt_cap_price = self._coerce_float(
                    getattr(state, "mktCapPrice", snapshot.mkt_cap_price)
                )
            if snapshot.submitted_at is None:
                snapshot.submitted_at = now
            snapshot.last_update = now

    def _handle_order_status(self, event: Mapping[str, Any]) -> None:
        order_id = self._coerce_int(event.get("order_id"))
        if order_id is None or order_id <= 0:
            return

        now = self._event_timestamp()
        with self._lock:
            snapshot = self._ensure_snapshot(order_id)
            status = event.get("status")
            if isinstance(status, str):
                snapshot.status = status
            snapshot.filled = self._coerce_float(event.get("filled"), snapshot.filled)
            snapshot.remaining = self._coerce_float(event.get("remaining"), snapshot.remaining)
            snapshot.avg_fill_price = self._coerce_float(
                event.get("avg_fill_price"), snapshot.avg_fill_price
            )
            snapshot.last_fill_price = self._coerce_float(
                event.get("last_fill_price"), snapshot.last_fill_price
            )
            snapshot.perm_id = self._coerce_int(event.get("perm_id"), snapshot.perm_id)
            snapshot.parent_id = self._coerce_int(event.get("parent_id"), snapshot.parent_id)
            snapshot.client_id = self._coerce_int(event.get("client_id"), snapshot.client_id)
            snapshot.why_held = event.get("why_held", snapshot.why_held)
            snapshot.mkt_cap_price = self._coerce_float(
                event.get("mkt_cap_price"), snapshot.mkt_cap_price
            )
            snapshot.last_update = now

    def _handle_execution(self, event: Mapping[str, Any]) -> None:
        execution = event.get("execution")
        if execution is None:
            return

        order_id = self._coerce_int(getattr(execution, "orderId", None))
        if order_id is None or order_id <= 0:
            return

        exec_id = getattr(execution, "execId", "") or ""
        fill = SimpleNamespace(execution=execution, commissionReport=None)

        now = self._event_timestamp()
        with self._lock:
            snapshot = self._ensure_snapshot(order_id)
            snapshot.fills.append(fill)
            snapshot.last_update = now
            self._exec_to_order[exec_id] = order_id
            self._exec_to_fill[exec_id] = fill

    def _handle_commission(self, event: Mapping[str, Any]) -> None:
        report = event.get("report")
        if report is None:
            return

        exec_id = getattr(report, "execId", "") or ""
        order_id = self._exec_to_order.get(exec_id)
        if order_id is None:
            return

        commission = self._coerce_float(getattr(report, "commission", None))
        pnl = self._coerce_float(getattr(report, "realizedPNL", None))

        with self._lock:
            snapshot = self._orders.get(order_id)
            if snapshot is None:
                return
            snapshot.commission = commission
            snapshot.realized_pnl = pnl
            fill = self._exec_to_fill.get(exec_id)
            if fill is not None:
                fill.commissionReport = report

        if self._commission_callback is not None:
            loop = self._loop_getter()
            if loop and not loop.is_closed():
                try:
                    coro = self._commission_callback(str(order_id), commission or 0.0, pnl or 0.0)
                    asyncio.run_coroutine_threadsafe(coro, loop)
                except Exception:
                    pass

    def _ensure_snapshot(self, order_id: int) -> _OrderSnapshot:
        snapshot = self._orders.get(order_id)
        if snapshot is None:
            now = self._event_timestamp()
            snapshot = _OrderSnapshot(
                order_id=order_id,
                contract=None,
                order=None,
                state=None,
                status="Submitted",
                filled=0.0,
                remaining=0.0,
                avg_fill_price=0.0,
                last_fill_price=0.0,
                perm_id=None,
                parent_id=None,
                client_id=None,
                why_held=None,
                mkt_cap_price=None,
                submitted_at=now,
                last_update=now,
            )
            self._orders[order_id] = snapshot
        return snapshot

    def _coerce_float(self, value: Any, fallback: float = 0.0) -> float:
        if value is None:
            return fallback
        try:
            cleaned = float(value)
        except (TypeError, ValueError):
            return fallback
        if math.isnan(cleaned) or math.isinf(cleaned):
            return fallback
        return cleaned

    def _coerce_int(self, value: Any, fallback: Optional[int] = None) -> Optional[int]:
        if value is None:
            return fallback
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback
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

    @staticmethod
    def _parse_env_int(key: str, default: int) -> int:
        raw = os.getenv(key)
        if raw is None:
            return int(default)

        try:
            cleaned = raw.split('#', 1)[0].strip()
        except Exception:
            return int(default)

        if cleaned == "":
            return int(default)

        try:
            return int(cleaned)
        except Exception:
            try:
                ibkr_logger.warning(f"Invalid integer for {key}='{raw}', falling back to {default}")
            except Exception:
                pass
            return int(default)

    def __init__(
        self,
        *,
        connection_manager: Optional[ConnectionManager] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        self.connection_manager = connection_manager or _GLOBAL_CONNECTION_MANAGER

        # Optional metadata describing the client purpose (e.g., primary/secondary)
        self.metadata: Dict[str, Any] = dict(metadata or {})

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

        # Track commission/PnL updates when the orders service is unavailable.
        self._order_fill_cache: Dict[str, Dict[str, Any]] = {}
        self._orders_service_client: Optional[OrdersServiceClient] = None

        # Counter for simulated order identifiers when trading is disabled
        self._simulated_order_id = itertools.count(start=-1, step=-1)

        self._trade_book = _TradeBook(
            loop_getter=self._get_async_loop,
            commission_callback=self._update_order_commission_pnl,
        )

        # Async bridge for contract qualification using the official ibapi client
        self._ibapi_controller: Optional[IBAPIController] = None
        self._ibapi_controller_lock = asyncio.Lock()
        self._contract_details_cache: Dict[Tuple[Any, ...], IBAPIContractDetails] = {}
        self._contract_details_inflight: Dict[Tuple[Any, ...], asyncio.Future[IBAPIContractDetails]] = {}
        self._contract_details_lock = asyncio.Lock()

        config = self.connection_manager.config
        self._ibapi_contract_client_id = self._parse_env_int(
            "IBAPI_CONTRACT_CLIENT_ID",
            max(config.client_id + 16, config.client_id + 1),
        )
        self._ibapi_contract_connect_timeout = max(
            1.0,
            self._parse_env_float("IBAPI_CONTRACT_CONNECT_TIMEOUT_SEC", 5.0),
        )
        self._ibapi_contract_request_timeout = max(
            1.0,
            self._parse_env_float("IBAPI_CONTRACT_REQUEST_TIMEOUT_SEC", 5.0),
        )
        self._ibapi_historical_request_timeout = max(
            1.0,
            self._parse_env_float(
                "IBAPI_HISTORICAL_REQUEST_TIMEOUT_SEC",
                self._ibapi_contract_request_timeout,
            ),
        )
        self._ibapi_order_submit_timeout = max(
            1.0,
            self._parse_env_float(
                "IBAPI_ORDER_SUBMIT_TIMEOUT_SEC",
                self._ibapi_contract_request_timeout,
            ),
        )
        self._ibapi_order_cancel_timeout = max(
            1.0,
            self._parse_env_float(
                "IBAPI_ORDER_CANCEL_TIMEOUT_SEC",
                self._ibapi_contract_request_timeout,
            ),
        )
        self._ibapi_account_request_timeout = max(
            1.0,
            self._parse_env_float(
                "IBAPI_ACCOUNT_REQUEST_TIMEOUT_SEC",
                self._ibapi_contract_request_timeout,
            ),
        )
        self._ibapi_account_lock = asyncio.Lock()
        self._ibapi_positions_lock = asyncio.Lock()
        self._ibapi_positions_request_timeout = max(
            1.0,
            self._parse_env_float(
                "IBAPI_POSITIONS_REQUEST_TIMEOUT_SEC",
                self._ibapi_account_request_timeout,
            ),
        )

        # --- Connection monitoring & resilience state ---
        self._async_loop: Optional[asyncio.AbstractEventLoop] = None
        self._connection_monitor_task: Optional[asyncio.Task] = None
        self._disconnect_time: Optional[datetime] = None
        self._reconnect_attempt: int = 0
        self._gateway_restart_triggered: bool = False
        self._self_restart_triggered: bool = False
        self._needs_cleanup: bool = False
        self._ensure_connection_lock = asyncio.Lock()

        self._monitor_interval_seconds = max(1.0, self._parse_env_float('IBKR_CONNECTION_MONITOR_INTERVAL', 5.0))
        connected_interval_default = max(15.0, self._monitor_interval_seconds)
        self._connected_monitor_interval = max(
            15.0,
            self._parse_env_float(
                'IBKR_CONNECTION_MONITOR_CONNECTED_INTERVAL',
                connected_interval_default,
            ),
        )
        self._monitor_wakeup_event: Optional[asyncio.Event] = None
        base_delay = self._parse_env_float('IBKR_RECONNECT_BASE_DELAY', 5.0) or 5.0
        self._reconnect_base_delay = max(0.5, base_delay)
        self._reconnect_max_delay = max(self._reconnect_base_delay, self._parse_env_float('IBKR_RECONNECT_MAX_DELAY', 60.0))
        self._gateway_restart_timeout = max(30.0, self._parse_env_float('IBKR_GATEWAY_RESTART_TIMEOUT', 120.0))
        self._self_restart_timeout = max(self._gateway_restart_timeout + 30.0, self._parse_env_float('IBKR_SELF_RESTART_TIMEOUT', 300.0))
        self._gateway_restart_cmd = os.getenv('IBKR_GATEWAY_RESTART_CMD')
        self._gateway_service_name = os.getenv('IBKR_GATEWAY_SERVICE_NAME', 'ib-gateway')
        self._wait_existing_connect_seconds = max(
            0.0,
            self._parse_env_float('IBKR_WAIT_FOR_EXISTING_CONNECT_SEC', 8.0),
        )

        self._data_farm_wakeup_lock = threading.Lock()
        self._data_farm_wakeup_future: Optional[concurrent.futures.Future] = None
        self._last_data_farm_wakeup: Optional[datetime] = None
        self._data_farm_wakeup_min_interval = max(
            15.0, self._parse_env_float('IBKR_DATA_FARM_WAKEUP_INTERVAL_SEC', 60.0)
        )
        self._data_farm_wakeup_symbol = os.getenv('IBKR_DATA_FARM_WAKEUP_SYMBOL', 'SPY')

        self._setup_async_components()

        # ---- Real-time market data streaming state (ib-feed integration) ----
        self._market_data_lock = threading.Lock()
        self._market_data_streams: Dict[str, _MarketDataSubscription] = {}
        self._market_data_router: Optional[MarketDataRouter] = None
        self._market_data_router_lock = asyncio.Lock()
        self._market_data_router_symbols: Set[str] = set()
        self._price_listeners: List[Callable[[str, Mapping[str, Any]], None]] = []
        self._dom_listeners: List[Callable[[str, Sequence[Mapping[str, Any]]], None]] = []
        self._disconnect_listeners: List[Callable[[], None]] = []
        self._default_dom_rows = int(self._parse_env_float('IBKR_DOM_ROWS', 10) or 10)

        # Track the last time we surfaced a historical-data connection loss per symbol
        self._historical_connection_loss_log: Dict[str, float] = {}
    
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

    def _should_log_historical_connection_loss(self, symbol: str, *, window: float = 60.0) -> bool:
        """Rate-limit repeated connection loss logs for the same symbol."""

        now = time.monotonic()
        last = self._historical_connection_loss_log.get(symbol)
        if last is not None and now - last < window:
            return False
        self._historical_connection_loss_log[symbol] = now
        return True

    def _clear_historical_connection_loss(self, symbol: str) -> None:
        """Reset the connection loss throttling for ``symbol``."""

        if symbol in self._historical_connection_loss_log:
            del self._historical_connection_loss_log[symbol]

    @property
    def connected(self) -> bool:
        """检查连接状态 - 为兼容性提供connected属性"""
        self._setup_async_components()
        return self.connection_manager.is_connected()
        
    def connect(self) -> bool:
        """连接到IBKR"""
        self._setup_async_components()
        previous_flag = getattr(self.connection_manager, "_ibkr_connect_in_progress", False)
        self.connection_manager._ibkr_connect_in_progress = True  # type: ignore[attr-defined]
        try:
            result = self.connection_manager.connect()
        finally:
            self.connection_manager._ibkr_connect_in_progress = previous_flag  # type: ignore[attr-defined]

        if result:
            self._handle_connection_restored()
        else:
            self._handle_disconnect_detected("connect-failed")
        return result
    
    async def disconnect(self) -> None:
        """断开连接并回收所有后台任务。"""

        await self._release_ibapi_market_data()
        await self._release_market_data_subscriptions()
        self._clear_market_data_streams()
        await self._shutdown_async_components()
        await asyncio.to_thread(self.connection_manager.disconnect)

    def is_connected(self) -> bool:
        """检查连接状态"""
        self._setup_async_components()
        return self.connection_manager.is_connected()

    def get_connection_manager(self) -> ConnectionManager:
        """Return the underlying connection manager instance."""

        return self.connection_manager

    def add_price_listener(self, callback: Callable[[str, Mapping[str, Any]], None]) -> None:
        """Register a listener for consolidated price updates."""

        if not callable(callback):
            raise TypeError("callback must be callable")
        with self._market_data_lock:
            if callback not in self._price_listeners:
                self._price_listeners.append(callback)

    def add_dom_listener(self, callback: Callable[[str, Sequence[Mapping[str, Any]]], None]) -> None:
        """Register a listener for DOM depth updates."""

        if not callable(callback):
            raise TypeError("callback must be callable")
        with self._market_data_lock:
            if callback not in self._dom_listeners:
                self._dom_listeners.append(callback)

    def add_disconnect_listener(self, callback: Callable[[], None]) -> None:
        """Register a listener invoked when the IB connection drops."""

        if not callable(callback):
            raise TypeError("callback must be callable")
        with self._market_data_lock:
            if callback not in self._disconnect_listeners:
                self._disconnect_listeners.append(callback)

    async def ensure_connection(self, *, reason: str = "ensure") -> None:
        """Ensure the IB connection is active before performing IB calls."""

        self._setup_async_components()

        manager = self.connection_manager
        try:
            if manager.is_connected():
                return
        except Exception:
            pass

        async with self._ensure_connection_lock:
            try:
                if manager.is_connected():
                    return
            except Exception:
                pass

            self._handle_disconnect_detected(reason)

            attempt_number = self._reconnect_attempt + 1
            success = await self._attempt_connection(attempt_number)
            if success:
                self._handle_connection_restored()
                return

            self._reconnect_attempt = attempt_number
            await self._after_failed_attempt()
            raise IBKRConnectionError("IBKR connection unavailable")

    def mark_connection_lost(self, reason: str = "external") -> None:
        """Manually mark the connection as lost without triggering reconnect."""

        self._handle_disconnect_detected(reason)

    async def _ensure_active_ib(self, *, reason: str) -> Optional[IB]:
        """Return a connected ``IB`` instance, reconnecting on demand."""

        ib = self.ib
        if ib and ib.isConnected():
            return ib

        # The connection manager may report success even though TWS/Gateway
        # immediately closes the socket because of a client ID conflict. When
        # that happens we see misleading "connection lost" errors in subsequent
        # commands. Mark the connection as stale and trigger another reconnect
        # attempt so callers either receive a healthy session or a consistent
        # failure.
        self.mark_connection_lost(f"{reason}-stale")

        try:
            await self.ensure_connection(reason=reason)
        except IBKRConnectionError:
            return None

        ib = self.ib
        if ib and ib.isConnected():
            return ib

        return None

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

    def _get_async_loop(self) -> Optional[asyncio.AbstractEventLoop]:
        loop = self._async_loop
        if loop is None or loop.is_closed():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return None
        return loop

    def _signal_monitor_wakeup(self) -> None:
        """Wake the connection monitor loop if it is waiting."""

        event = self._monitor_wakeup_event
        if event is None:
            return

        loop = self._async_loop
        if loop and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(event.set)
                return
            except RuntimeError:
                if loop.is_running():
                    event.set()
                    return

        try:
            event.set()
        except Exception:
            pass

    def _on_connection_manager_connected(self, *, external: bool) -> None:
        """ConnectionManager callback when a connection succeeds."""

        if not external:
            return

        self._setup_async_components()
        try:
            self._handle_connection_restored(initial=True)
        except Exception as exc:  # pragma: no cover - defensive guard
            ibkr_logger.debug(
                "Failed to finalize external connection restoration",
                error=str(exc),
            )

    def handle_data_farm_status(self, code: int, message: str) -> None:
        """Handle data farm status notifications by issuing a gentle wake-up request."""

        ibkr_logger.info(
            "IBKR data farm status update",
            code=code,
            message=message,
        )
        self._schedule_data_farm_wakeup(code=code, message=message)

    def handle_gateway_broken(self, code: int, message: str) -> None:
        """Schedule recovery when IB reports a broken data farm connection."""

        loop = self._async_loop
        if loop is None or loop.is_closed():
            ibkr_logger.debug(
                "Skipping gateway-broken handling; async loop unavailable",
                code=code,
            )
            return

        def _dispatch() -> None:
            ibkr_logger.warning(
                "IBKR gateway reported broken connection",
                code=code,
                message=message,
            )
            self._handle_disconnect_detected(f"ib-error-{code}")

        try:
            loop.call_soon_threadsafe(_dispatch)
        except RuntimeError:
            if loop.is_running():
                _dispatch()
            else:
                ibkr_logger.debug(
                    "Async loop not running; cannot dispatch gateway-broken handler",
                    code=code,
                )

    def _schedule_data_farm_wakeup(self, *, code: int, message: str) -> None:
        loop = self._async_loop
        if loop is None or loop.is_closed():
            ibkr_logger.debug(
                "Skipping data farm wake-up scheduling; async loop unavailable",
                code=code,
            )
            return

        now = now_local()
        future: Optional[concurrent.futures.Future]
        with self._data_farm_wakeup_lock:
            if self._data_farm_wakeup_future and not self._data_farm_wakeup_future.done():
                ibkr_logger.debug(
                    "Skipping data farm wake-up; previous request still pending",
                    code=code,
                )
                return

            if self._last_data_farm_wakeup is not None:
                elapsed = (now - self._last_data_farm_wakeup).total_seconds()
                if elapsed < self._data_farm_wakeup_min_interval:
                    ibkr_logger.debug(
                        "Skipping data farm wake-up; throttled",
                        code=code,
                        elapsed_seconds=round(elapsed, 1),
                    )
                    return

            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._send_data_farm_wakeup(code=code, message=message),
                    loop,
                )
            except RuntimeError as exc:
                ibkr_logger.debug(
                    "Failed to submit data farm wake-up coroutine",
                    code=code,
                    error=str(exc),
                )
                return

            self._data_farm_wakeup_future = future
            self._last_data_farm_wakeup = now

        def _finalise(fut: concurrent.futures.Future) -> None:
            with self._data_farm_wakeup_lock:
                if self._data_farm_wakeup_future is fut:
                    self._data_farm_wakeup_future = None
            try:
                fut.result()
            except Exception as exc:  # pragma: no cover - defensive logging
                ibkr_logger.debug(
                    "Data farm wake-up request completed with error",
                    code=code,
                    error=str(exc),
                )

        future.add_done_callback(_finalise)

    async def _send_data_farm_wakeup(self, *, code: int, message: str) -> None:
        controller = await self._ensure_ibapi_controller()
        if controller is None or not controller.is_connected():
            ibkr_logger.debug(
                "Skipping data farm wake-up; IBAPI controller unavailable",
                code=code,
            )
            return

        symbol = (self._data_farm_wakeup_symbol or "SPY").strip() or "SPY"
        symbol = symbol.upper()
        contract = self.get_stock_contract(symbol, 'SMART')
        qualified_contract = contract

        qualified_contract = await self._qualify_contract(
            contract,
            symbol=symbol,
            exchange=getattr(contract, "exchange", "SMART") or "SMART",
            currency=getattr(contract, "currency", "USD") or "USD",
        ) or contract

        try:
            api_contract = self._convert_to_ibapi_contract(qualified_contract)
            await controller.request_historical_data(
                api_contract,
                end_datetime='',
                duration='60 S',
                bar_size='1 min',
                what_to_show='MIDPOINT',
                use_rth=False,
                keep_up_to_date=False,
                format_date=1,
                timeout=self._ibapi_historical_request_timeout,
            )
            ibkr_logger.debug(
                "Submitted data farm wake-up historical request",
                code=code,
                symbol=symbol,
            )
        except Exception as exc:
            ibkr_logger.debug(
                "Data farm wake-up historical request failed",
                code=code,
                symbol=symbol,
                error=str(exc),
            )

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

        wake_event = asyncio.Event()
        self._monitor_wakeup_event = wake_event

        if previous_state:
            self._handle_connection_restored(initial=True, signal_monitor=False)
        else:
            self._handle_disconnect_detected("monitor-start", signal_monitor=False)

        connected_interval = self._connected_monitor_interval
        disconnected_interval = self._monitor_interval_seconds

        try:
            while True:
                timeout = connected_interval if previous_state else disconnected_interval
                try:
                    await asyncio.wait_for(wake_event.wait(), timeout=timeout)
                    wake_event.clear()
                except asyncio.TimeoutError:
                    pass
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    ibkr_logger.debug(f"Monitor wait interrupted: {exc}")
                    continue

                try:
                    connected = self.is_connected()
                except Exception as exc:
                    ibkr_logger.warning(f"IBKR connection check failed: {exc}")
                    connected = False

                if connected:
                    if not previous_state:
                        self._handle_connection_restored(signal_monitor=False)
                    previous_state = True
                    continue

                # Connection is down
                if previous_state:
                    self._handle_disconnect_detected("connection-lost", signal_monitor=False)
                previous_state = False
        except asyncio.CancelledError:
            ibkr_logger.debug("IBKR connection monitor cancelled")
            raise
        finally:
            self._monitor_wakeup_event = None

    async def _shutdown_async_components(self) -> None:
        """Cancel background tasks and disconnect auxiliary controllers."""

        with self._data_farm_wakeup_lock:
            future = self._data_farm_wakeup_future
            self._data_farm_wakeup_future = None
        if future is not None:
            future.cancel()
            if future.done():
                with suppress(Exception):
                    future.result()

        await self._trade_book.detach()

        router = self._market_data_router
        if router is not None:
            try:
                await router.shutdown()
            except Exception as exc:  # pragma: no cover - defensive guard
                ibkr_logger.debug(
                    "Market data router shutdown failed",
                    error=str(exc),
                )
            finally:
                self._market_data_router = None

        monitor_task = self._connection_monitor_task
        if monitor_task is not None:
            monitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await monitor_task
            self._connection_monitor_task = None

        if self._monitor_wakeup_event is not None:
            try:
                self._monitor_wakeup_event.set()
            except Exception:
                pass
            self._monitor_wakeup_event = None

        controller = self._ibapi_controller
        if controller is not None:
            try:
                await controller.disconnect()
            except Exception as exc:  # pragma: no cover - defensive guard
                ibkr_logger.debug(
                    "Failed to disconnect IBAPI controller during shutdown",
                    error=str(exc),
                )

    def _handle_disconnect_detected(self, trigger: str, *, signal_monitor: bool = True) -> None:
        now = now_local()
        if self._disconnect_time is None:
            self._disconnect_time = now
            self._reconnect_attempt = 0
            self._gateway_restart_triggered = False
            self._self_restart_triggered = False
            self._needs_cleanup = True
            ibkr_logger.warning(
                "IBKR connection lost",
                trigger=trigger,
            )
        else:
            ibkr_logger.debug(
                "IBKR connection still unavailable",
                trigger=trigger,
                attempts=self._reconnect_attempt,
            )

        if signal_monitor:
            self._signal_monitor_wakeup()
        self._notify_disconnect_listeners()
        self._clear_market_data_streams()

    def _handle_connection_restored(self, *, initial: bool = False, signal_monitor: bool = True) -> None:
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

        if signal_monitor:
            self._signal_monitor_wakeup()

    async def _attempt_connection(self, attempt_number: int) -> bool:
        if self._needs_cleanup:
            try:
                await asyncio.to_thread(self.connection_manager.disconnect)
            except Exception as exc:
                ibkr_logger.debug(f"Cleanup disconnect failed: {exc}")
            finally:
                self._needs_cleanup = False

        manager = self.connection_manager
        try:
            if manager.is_connected():
                return True
        except Exception:
            pass

        try:
            if manager.is_connecting():
                waited = await asyncio.to_thread(
                    manager.wait_for_connection,
                    self._wait_existing_connect_seconds,
                )
                if waited:
                    return True
        except Exception as exc:
            ibkr_logger.debug(
                "Waiting on existing IBKR connection attempt failed",
                error=str(exc),
            )

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
            compose_file = self.connection_manager._project_root / "docker" / "local.compose.yml"
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
            compose_file = self.connection_manager._project_root / "docker" / "local.compose.yml"
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
    
    async def _place_order_via_ibapi_controller(
        self,
        *,
        symbol: str,
        quantity: int,
        action: str,
        exchange: str,
        sec_type: str,
        order_type: str,
        limit_price: Optional[float],
    ) -> Any | None:
        controller = await self._ensure_ibapi_controller()
        if controller is None or not controller.is_connected():
            return None

        try:
            contract = self.get_contract(symbol, sec_type, exchange)
        except Exception as exc:
            ibkr_logger.debug("Failed to build contract for ibapi order: {}", exc)
            return None

        currency = getattr(contract, "currency", "USD") or "USD"
        qualified_contract = await self._qualify_contract(
            contract,
            symbol=symbol,
            exchange=getattr(contract, "exchange", exchange) or exchange,
            currency=currency,
        )
        if not qualified_contract:
            return None

        api_contract = self._convert_to_ibapi_contract(qualified_contract)

        try:
            ib_order = self._build_ibapi_order(
                action=action,
                quantity=quantity,
                order_type=order_type,
                limit_price=limit_price,
            )
        except Exception as exc:
            ibkr_logger.debug("Failed to construct ibapi order: {}", exc)
            return None

        try:
            placement = await controller.place_order(
                api_contract,
                ib_order,
                timeout=self._ibapi_order_submit_timeout,
            )
        except Exception as exc:
            ibkr_logger.debug("Failed to place order via ibapi controller: {}", exc)
            return None

        self._trade_book.record_placement(placement)

        return self._build_trade_from_placement(
            placement,
            quantity=quantity,
            status_hint="Submitted",
        )

    def _build_ibapi_order(
        self,
        *,
        action: str,
        quantity: int,
        order_type: str,
        limit_price: Optional[float],
    ) -> IBAPIOrder:
        order = IBAPIOrder()
        try:
            order.action = action
        except Exception:
            pass
        try:
            order.totalQuantity = quantity
        except Exception:
            pass

        canonical = order_type.upper()
        if canonical == "MARKET":
            value = "MKT"
        else:
            value = "LMT"
            if limit_price is not None:
                try:
                    order.lmtPrice = float(limit_price)
                except Exception:
                    order.lmtPrice = limit_price
        try:
            order.orderType = value
        except Exception:
            pass

        if hasattr(order, "tif") and not getattr(order, "tif", None):
            try:
                order.tif = "GTC"
            except Exception:
                pass
        if hasattr(order, "transmit"):
            try:
                order.transmit = True
            except Exception:
                pass
        return order

    def _build_trade_from_placement(
        self,
        placement: OrderPlacement,
        *,
        quantity: int,
        status_hint: str,
    ) -> Any:
        state = placement.state
        if state is None:
            state = IBAPIOrderState()  # type: ignore[call-arg]
            try:
                state.status = status_hint or "Submitted"
            except Exception:
                pass
            try:
                state.filled = 0.0
            except Exception:
                pass
            try:
                state.remaining = float(quantity)
            except Exception:
                pass
        else:
            try:
                remaining = getattr(state, "remaining", None)
                if remaining in (None, ""):
                    state.remaining = float(quantity)
            except Exception:
                pass

        trade = SimpleNamespace(
            contract=placement.contract,
            order=placement.order,
            orderStatus=state,
            fills=[],
        )
        return trade

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

    def _normalise_trade_result(
        self,
        trade: Any,
        *,
        symbol: str,
        action: str,
        quantity: float,
        order_type: str,
        limit_price: Optional[float],
        exchange: Optional[str],
        sec_type: Optional[str],
    ) -> Dict[str, Any]:
        """Convert raw trade-like payloads into a normalised mapping."""

        defaults: Dict[str, Any] = {
            "symbol": symbol,
            "action": action,
            "quantity": self._sanitize_float(quantity),
            "order_type": order_type,
        }
        if exchange:
            defaults["exchange"] = exchange
        if sec_type:
            defaults["sec_type"] = str(sec_type).upper()
        if limit_price is not None:
            defaults["limit_price"] = self._sanitize_float(limit_price)

        order_id_value: Optional[Any] = None
        if isinstance(trade, Mapping):
            if "order_id" in trade and trade["order_id"] is not None:
                order_id_value = trade["order_id"]
            result: Dict[str, Any] = dict(trade)
        else:
            order = getattr(trade, "order", None)
            if order is not None:
                order_id_value = getattr(order, "orderId", None) or getattr(order, "permId", None)
            result = trade_to_dict(trade)

        if order_id_value is not None:
            try:
                order_id_str = str(order_id_value)
            except Exception:
                order_id_str = None
            else:
                if order_id_str:
                    defaults.setdefault("order_id", order_id_str)

        for key, value in defaults.items():
            if result.get(key) is None:
                result[key] = value
            elif key == "order_type":
                # Ensure canonical order type strings (e.g. ``LIMIT`` over ``LMT``).
                result[key] = value

        submitted_at = result.get("submitted_at")
        if submitted_at and isinstance(submitted_at, str):
            try:
                result["submitted_at"] = to_local_time(submitted_at)
            except Exception:
                pass

        return result

    async def place_order(self, **kwargs: Any) -> Dict[str, Any]:
        """High-level entry point for market and limit orders.

        Parameters mirror :class:`services.orders.main.OrderSubmission` so that
        callers can forward validated payloads directly to the client.
        """

        symbol = str(kwargs.get("symbol") or "").strip()
        if not symbol:
            raise ValueError("place_order requires 'symbol'")

        raw_quantity = kwargs.get("quantity")
        if raw_quantity is None:
            raw_quantity = kwargs.get("total_quantity")
        quantity_value = self._sanitize_float(raw_quantity)
        if quantity_value is None:
            raise ValueError("place_order requires numeric 'quantity'")
        if quantity_value <= 0:
            raise ValueError("place_order requires a positive quantity")
        if isinstance(quantity_value, float) and not quantity_value.is_integer():
            raise ValueError("place_order requires an integer quantity")
        quantity_int = int(round(quantity_value))

        action = str(kwargs.get("action") or "").strip().upper()
        if not action:
            raise ValueError("place_order requires 'action'")

        order_type_raw = kwargs.get("order_type") or kwargs.get("type") or "MARKET"
        order_type_normalized = str(order_type_raw).strip().upper()
        if order_type_normalized in {"MARKET", "MKT"}:
            canonical_order_type = "MARKET"
        elif order_type_normalized in {"LIMIT", "LMT"}:
            canonical_order_type = "LIMIT"
        else:
            raise ValueError(f"Unsupported order_type '{order_type_raw}'")

        limit_price = kwargs.get("limit_price")
        if limit_price is None:
            for key in ("lmt_price", "price", "limitPrice"):
                if key in kwargs and kwargs[key] is not None:
                    limit_price = kwargs[key]
                    break
        limit_price_value = self._sanitize_float(limit_price)
        if canonical_order_type == "LIMIT" and limit_price_value is None:
            raise ValueError("limit orders require 'limit_price'")

        exchange = str(kwargs.get("exchange") or "SMART").strip() or "SMART"
        sec_type = kwargs.get("sec_type") or kwargs.get("security_type")
        if not sec_type and symbol.upper() in INDEX_FUTURE_SYMBOLS:
            sec_type = "FUT"
        sec_type = str(sec_type or "STK").strip().upper()

        try:
            if is_trading_disabled():
                trade = self._simulate_trade(
                    order_type=canonical_order_type,
                    symbol=symbol,
                    quantity=quantity_int,
                    action=action,
                    exchange=exchange,
                    sec_type=sec_type,
                    price=limit_price_value,
                )
            elif canonical_order_type == "MARKET":
                trade = await self.place_market_order(
                    symbol=symbol,
                    quantity=quantity_int,
                    action=action,
                    exchange=exchange,
                    sec_type=sec_type,
                )
            else:
                trade = await self.place_limit_order(
                    symbol=symbol,
                    quantity=quantity_int,
                    price=limit_price_value,
                    action=action,
                    exchange=exchange,
                    sec_type=sec_type,
                )
        except Exception as exc:
            ibkr_logger.error(
                "Failed to place {} order for {}: {}",
                canonical_order_type.lower(),
                symbol,
                exc,
            )
            raise

        if trade is None:
            raise RuntimeError("IBKR returned no trade information for order")

        return self._normalise_trade_result(
            trade,
            symbol=symbol,
            action=action,
            quantity=quantity_value,
            order_type=canonical_order_type,
            limit_price=limit_price_value,
            exchange=exchange,
            sec_type=sec_type,
        )

    def get_stock_contract(
        self,
        symbol: str,
        exchange: str = 'SMART',
        currency: str = 'USD',
    ) -> IBAPIContract:
        """获取股票合约"""

        contract = IBAPIContract()
        contract.symbol = symbol
        contract.secType = 'STK'
        contract.exchange = exchange
        contract.currency = currency
        return contract

    def _create_future_contract(self, symbol: str, exchange: str, currency: str) -> IBAPIContract:
        """Construct a future contract using the official IBAPI data model."""

        contract = IBAPIContract()
        contract.symbol = symbol
        contract.secType = 'FUT'
        contract.exchange = exchange
        contract.currency = currency
        return contract

    def _future_contract_candidates(
        self,
        symbol: str,
        exchange: str,
        currency: str,
    ) -> List[IBAPIContract]:
        """Generate a set of plausible future contracts for qualification.

        Some index futures (notably the Russell micro contract ``M2K``) have a
        richer month cycle than the standard quarterly series.  When we guess an
        expiry that is not yet listed at IBKR the resulting qualification call
        fails with ``200 / No security definition``.  To make the system more
        resilient we build a list of alternative contracts that cover the
        current and next year's available months (with and without optional
        attributes like ``tradingClass``), plus a last-resort variant without an
        explicit expiry.
        """

        symbol_upper = symbol.upper()
        details = INDEX_FUTURE_DETAILS.get(symbol_upper, {})
        exchange_to_use = details.get("exchange", exchange)
        months = _coerce_contract_months(details.get("contract_months"))
        current_now = now_local()
        ny_now = current_now.astimezone(NY_TZ)
        base_year = ny_now.year
        base_month = ny_now.month

        candidates: List[IBAPIContract] = []
        seen: set[Tuple[str, str, str, str, Optional[str], Optional[str]]] = set()

        def _append_candidate(contract: IBAPIContract) -> None:
            signature = (
                getattr(contract, "symbol", ""),
                getattr(contract, "exchange", ""),
                getattr(contract, "currency", ""),
                getattr(contract, "lastTradeDateOrContractMonth", ""),
                getattr(contract, "tradingClass", None),
                getattr(contract, "multiplier", None),
            )
            if signature in seen:
                return
            seen.add(signature)
            candidates.append(contract)

        def _build_contract(expiry: Optional[str], *, with_details: bool) -> IBAPIContract:
            contract = self._create_future_contract(symbol_upper, exchange_to_use, currency)
            if expiry:
                contract.lastTradeDateOrContractMonth = expiry
            if with_details:
                for attr in ("multiplier", "tradingClass"):
                    value = details.get(attr)
                    if value:
                        setattr(contract, attr, value)
            return contract

        # Default guess based on the configured contract cycle
        front_year, front_month = _front_contract(current_now, months)
        default_expiry = f"{front_year}{front_month:02d}"
        _append_candidate(_build_contract(default_expiry, with_details=True))

        # Iterate through the remaining months of this year and the next two
        # years to cover serial-month products (e.g. M2K).
        for year_offset in range(0, 3):
            candidate_year = base_year + year_offset
            for candidate_month in months:
                if year_offset == 0 and candidate_month < base_month:
                    continue
                expiry = f"{candidate_year}{candidate_month:02d}"
                _append_candidate(_build_contract(expiry, with_details=True))
                _append_candidate(_build_contract(expiry, with_details=False))

        # Fallbacks without an explicit expiry allow IBKR to pick the front
        # contract automatically.
        _append_candidate(_build_contract(None, with_details=True))
        _append_candidate(_build_contract(None, with_details=False))

        return candidates

    async def _ensure_ibapi_controller(self) -> Optional[IBAPIController]:
        controller = self._ibapi_controller
        if controller and controller.is_connected():
            await self._trade_book.attach(controller)
            return controller

        async with self._ibapi_controller_lock:
            controller = self._ibapi_controller
            if controller and controller.is_connected():
                await self._trade_book.attach(controller)
                return controller

            try:
                loop = self._async_loop or asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.get_event_loop()

            params = ConnectionParams(
                host=self.connection_manager.config.host,
                port=self.connection_manager.config.port,
                client_id=self._ibapi_contract_client_id,
            )

            controller = IBAPIController(loop=loop)
            try:
                await controller.connect(params=params, timeout=self._ibapi_contract_connect_timeout)
                await controller.wait_until_connected(timeout=self._ibapi_contract_connect_timeout)
            except Exception as exc:
                try:
                    await controller.disconnect()
                except Exception:
                    pass
                self._ibapi_controller = None
                ibkr_logger.debug(
                    "Failed to initialise ibapi controller for contract details: {}",
                    exc,
                )
                return None

            self._ibapi_controller = controller
            await self._trade_book.attach(controller)
            return controller

    async def _ensure_market_data_router(self) -> Optional[MarketDataRouter]:
        router = self._market_data_router
        if router is not None:
            return router

        async with self._market_data_router_lock:
            router = self._market_data_router
            if router is not None:
                return router

            try:
                loop = self._async_loop or asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.get_event_loop()

            router = MarketDataRouter(
                controller_getter=self._ensure_ibapi_controller,
                loop=loop,
                price_listener=self._notify_price_listeners,
                depth_listener=self._notify_dom_listeners,
                disconnect_listener=self._notify_disconnect_listeners,
            )
            self._market_data_router = router
            return router

    async def _request_account_updates_via_controller(self) -> Optional[dict[str, Any]]:
        controller = await self._ensure_ibapi_controller()
        if controller is None:
            return None

        async with self._ibapi_account_lock:
            configured_account = getattr(self.connection_manager.config, "account", "")
            try:
                snapshot = await controller.request_account_updates(
                    account=configured_account,
                    timeout=self._ibapi_account_request_timeout,
                )
            except asyncio.TimeoutError:
                ibkr_logger.debug(
                    "Timed out waiting for account updates via ibapi controller (account=%s, managed_accounts=%s)",
                    configured_account or "<default>",
                    ",".join(controller.managed_accounts) or "<none>",
                )
                return None
            except Exception as exc:
                ibkr_logger.debug(
                    "Failed to request account updates via ibapi controller: {}",
                    exc,
                )
                return None

        return snapshot

    async def _request_account_summary_via_controller(self) -> Optional[List[dict[str, Any]]]:
        controller = await self._ensure_ibapi_controller()
        if controller is None:
            return None

        async with self._ibapi_account_lock:
            try:
                summary = await controller.request_account_summary(
                    group="All",
                    tags="All",
                    timeout=self._ibapi_account_request_timeout,
                )
            except Exception as exc:
                ibkr_logger.debug(
                    "Failed to request account summary via ibapi controller: {}",
                    exc,
                )
                return None

        return summary

    async def _request_positions_via_controller(self) -> Optional[List[dict[str, Any]]]:
        controller = await self._ensure_ibapi_controller()
        if controller is None:
            return None

        async with self._ibapi_positions_lock:
            try:
                positions = await controller.request_positions(
                    timeout=self._ibapi_positions_request_timeout,
                )
            except Exception as exc:
                ibkr_logger.debug(
                    "Failed to request positions via ibapi controller: {}",
                    exc,
                )
                return None

        return positions

    def _contract_cache_key(self, contract: Any) -> Tuple[Any, ...]:
        return (
            getattr(contract, "secType", None),
            getattr(contract, "symbol", None),
            getattr(contract, "currency", None),
            getattr(contract, "exchange", None),
            getattr(contract, "primaryExchange", None),
            getattr(contract, "lastTradeDateOrContractMonth", None),
            getattr(contract, "tradingClass", None),
            getattr(contract, "multiplier", None),
            getattr(contract, "localSymbol", None),
        )

    def _convert_to_ibapi_contract(self, contract: Any) -> IBAPIContract:
        if isinstance(contract, IBAPIContract):
            return contract

        api_contract = IBAPIContract()
        for attr in (
            "conId",
            "secType",
            "symbol",
            "exchange",
            "primaryExchange",
            "currency",
            "lastTradeDateOrContractMonth",
            "tradingClass",
            "multiplier",
            "localSymbol",
            "includeExpired",
            "secIdType",
            "secId",
        ):
            if hasattr(contract, attr):
                try:
                    setattr(api_contract, attr, getattr(contract, attr))
                except Exception:
                    pass
        return api_contract

    def _convert_to_ib_insync_contract(self, contract: IBAPIContract) -> Any:
        """Convert an IBAPI contract into an ``ib_insync`` contract if available."""

        spec = importlib.util.find_spec("ib_insync")
        if spec is None:
            return contract

        module = importlib.import_module("ib_insync")
        contract_cls = getattr(module, "Contract", None)
        if contract_cls is None:
            return contract

        attrs = (
            "conId",
            "secType",
            "symbol",
            "exchange",
            "primaryExchange",
            "currency",
            "lastTradeDateOrContractMonth",
            "tradingClass",
            "multiplier",
            "localSymbol",
            "includeExpired",
            "secIdType",
            "secId",
        )
        kwargs: dict[str, Any] = {}
        for attr in attrs:
            value = getattr(contract, attr, None)
            if value not in (None, ""):
                kwargs[attr] = value

        try:
            return contract_cls(**kwargs)
        except Exception:
            stub = contract_cls()
            for attr, value in kwargs.items():
                try:
                    setattr(stub, attr, value)
                except Exception:
                    pass
            return stub

    def _apply_contract_details(
        self,
        base: IBAPIContract,
        details: IBAPIContractDetails,
    ) -> IBAPIContract:
        api_contract = getattr(details, "contract", None)
        if api_contract is None:
            return base

        for attr in (
            "conId",
            "secType",
            "symbol",
            "exchange",
            "primaryExchange",
            "currency",
            "lastTradeDateOrContractMonth",
            "tradingClass",
            "multiplier",
            "localSymbol",
        ):
            try:
                value = getattr(api_contract, attr)
            except Exception:
                continue
            try:
                setattr(base, attr, value)
            except Exception:
                pass

        return base

    async def _get_contract_details_from_cache(
        self,
        contract: IBAPIContract,
    ) -> Optional[IBAPIContractDetails]:
        controller = await self._ensure_ibapi_controller()
        if controller is None:
            return None

        key = self._contract_cache_key(contract)
        loop = asyncio.get_running_loop()

        async with self._contract_details_lock:
            cached = self._contract_details_cache.get(key)
            if cached is not None:
                return cached

            future = self._contract_details_inflight.get(key)
            if future is None:
                future = loop.create_future()
                self._contract_details_inflight[key] = future
                requester = True
            else:
                requester = False

        if requester:
            try:
                api_contract = self._convert_to_ibapi_contract(contract)
                details_list = await controller.request_contract_details(
                    api_contract,
                    timeout=self._ibapi_contract_request_timeout,
                )
                if not details_list:
                    raise IBAPIRequestError("No contract details returned")
                detail = details_list[0]
            except Exception as exc:
                if not future.done():
                    future.set_exception(exc)
            else:
                if not future.done():
                    future.set_result(detail)

        try:
            detail = await future
        finally:
            async with self._contract_details_lock:
                current = self._contract_details_inflight.get(key)
                if current is future:
                    self._contract_details_inflight.pop(key, None)
                if future.done() and not future.cancelled() and future.exception() is None:
                    self._contract_details_cache[key] = future.result()

        return detail

    async def _qualify_contract(
        self,
        contract: IBAPIContract,
        *,
        symbol: str,
        exchange: str,
        currency: str,
    ) -> IBAPIContract | None:
        """Qualify a contract, retrying with sensible fallbacks for futures."""

        controller = await self._ensure_ibapi_controller()
        if controller is None or not controller.is_connected():
            ibkr_logger.debug(
                "IBAPI controller unavailable for contract qualification",
                symbol=symbol,
                exchange=exchange,
            )
            return None

        symbol_upper = symbol.upper()
        candidates: Sequence[IBAPIContract]
        if getattr(contract, "secType", "").upper() == "FUT":
            candidates = self._future_contract_candidates(symbol_upper, exchange, currency)
        else:
            candidates = (contract,)

        last_exception: Exception | None = None
        for candidate in candidates:
            details: IBAPIContractDetails | None = None
            try:
                details = await self._get_contract_details_from_cache(candidate)
            except Exception as exc:
                last_exception = exc

            if details is not None:
                return self._apply_contract_details(candidate, details)

        if last_exception is not None:
            ibkr_logger.debug(
                "Contract qualification failed after attempting fallbacks",
                symbol=symbol_upper,
                exchange=exchange,
                error=str(last_exception),
            )

        return None

    def get_future_contract(self, symbol: str, exchange: str = 'SMART', currency: str = 'USD'):
        """获取期货合约"""

        symbol_upper = symbol.upper()
        if symbol_upper in INDEX_FUTURE_SYMBOLS:
            now = now_local()
            details = INDEX_FUTURE_DETAILS[symbol_upper]
            months = _coerce_contract_months(details.get("contract_months"))
            contract_year, contract_month = _front_contract(now, months)
            expiry = f"{contract_year}{contract_month:02d}"

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
    
    @staticmethod
    def _parse_bar_size_seconds(value: str) -> int | None:
        if not value:
            return None
        text = value.strip().lower()
        if not text:
            return None

        fixed = {
            '1 sec': 1,
            '1 secs': 1,
            '1 second': 1,
            '1 seconds': 1,
            '5 sec': 5,
            '5 secs': 5,
            '5 second': 5,
            '5 seconds': 5,
            '15 sec': 15,
            '15 secs': 15,
            '15 second': 15,
            '15 seconds': 15,
            '30 sec': 30,
            '30 secs': 30,
            '30 second': 30,
            '30 seconds': 30,
            '1 min': 60,
            '1 mins': 60,
            '1 minute': 60,
            '1 minutes': 60,
            '5 min': 300,
            '5 mins': 300,
            '5 minute': 300,
            '5 minutes': 300,
            '15 min': 900,
            '15 mins': 900,
            '15 minute': 900,
            '15 minutes': 900,
            '30 min': 1800,
            '30 mins': 1800,
            '30 minute': 1800,
            '30 minutes': 1800,
            '1 hour': 3600,
            '1 hours': 3600,
            '1 hr': 3600,
            '1 hrs': 3600,
            '1 day': 86400,
            '1 days': 86400,
        }
        if text in fixed:
            return fixed[text]

        match = re.match(r"(\d+(?:\.\d+)?)\s*([a-zA-Z]+)", text)
        if not match:
            return None

        try:
            magnitude = float(match.group(1))
        except (TypeError, ValueError):
            return None

        unit = match.group(2).lower()
        if unit in {'s', 'sec', 'secs', 'second', 'seconds'}:
            return int(magnitude)
        if unit in {'m', 'min', 'mins', 'minute', 'minutes'}:
            return int(magnitude * 60)
        if unit in {'h', 'hr', 'hrs', 'hour', 'hours'}:
            return int(magnitude * 3600)
        if unit in {'d', 'day', 'days'}:
            return int(magnitude * 86400)
        return None

    @staticmethod
    def _coerce_ib_datetime(value: datetime | str | None) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            dt = value
        else:
            text = str(value).strip()
            if not text:
                return None

            tzinfo: ZoneInfo | None = None
            base_text = text
            parts = text.split()
            if len(parts) > 2:
                tz_candidate = parts[-1]
                try:
                    tzinfo = ZoneInfo(tz_candidate)
                except Exception:
                    tzinfo = None
                else:
                    base_text = " ".join(parts[:-1])

            dt: datetime | None = None
            iso_candidate = base_text
            if iso_candidate.endswith('Z'):
                iso_candidate = iso_candidate[:-1] + '+00:00'
            try:
                dt = datetime.fromisoformat(iso_candidate)
            except ValueError:
                dt = None

            if dt is None:
                patterns = (
                    "%Y%m%d %H:%M:%S",
                    "%Y%m%d-%H:%M:%S",
                    "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%dT%H:%M:%S.%f",
                )
                for pattern in patterns:
                    try:
                        dt = datetime.strptime(base_text, pattern)
                    except ValueError:
                        continue
                    else:
                        break

            if dt is None:
                return None

            if tzinfo is not None:
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=tzinfo)
                else:
                    dt = dt.astimezone(tzinfo)
            elif dt.tzinfo is None:
                dt = dt.replace(tzinfo=NY_TZ)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=NY_TZ)
        return dt

    def _format_ib_end_datetime(self, value: datetime | str) -> str:
        coerced = self._coerce_ib_datetime(value)
        if coerced is None:
            return ""
        utc_value = coerced.astimezone(timezone.utc)
        return utc_value.strftime("%Y%m%d-%H:%M:%S")

    async def get_historical_data(
        self,
        symbol: str,
        duration: str | None = '1 Y',
        bar_size: str = '1 day',
        exchange: str = 'SMART',
        *,
        end_datetime: datetime | str | None = None,
        bars: int | None = None,
    ) -> pd.DataFrame:
        """获取历史数据（在连接线程事件循环中执行，避免事件循环错误）"""
        try:
            controller = await self._ensure_ibapi_controller()
            if controller is None or not controller.is_connected():
                raise IBKRConnectionError(f"IBAPI controller unavailable for historical data: {symbol}")

            symbol_upper = symbol.upper()
            if symbol_upper in INDEX_FUTURE_SYMBOLS:
                contract = self.get_future_contract(symbol_upper, 'CME')
            else:
                contract = self.get_stock_contract(symbol, exchange)

            self._clear_historical_connection_loss(symbol)

            qualified_contract = await self._qualify_contract(
                contract,
                symbol=symbol,
                exchange=getattr(contract, "exchange", exchange) or exchange,
                currency=getattr(contract, "currency", "USD") or "USD",
            )
            if not qualified_contract:
                ibkr_logger.error(f"Failed to qualify contract for {symbol}")
                return pd.DataFrame()

            end_value = ''
            if end_datetime is not None:
                end_value = self._format_ib_end_datetime(end_datetime)

            duration_str = (duration or '') if duration is not None else ''
            if bars is not None:
                try:
                    bar_seconds = self._parse_bar_size_seconds(bar_size)
                except Exception:
                    bar_seconds = None
                if bar_seconds:
                    try:
                        bar_count = max(int(bars), 1)
                    except Exception:
                        bar_count = 1
                    total_seconds = max(bar_seconds, bar_count * bar_seconds)
                    duration_str = f"{int(total_seconds)} S"
                elif not duration_str:
                    duration_str = str(bars)

            if not duration_str:
                duration_str = '1 Y'

            api_contract = self._convert_to_ibapi_contract(qualified_contract)
            try:
                historical_bars = await controller.request_historical_data(
                    api_contract,
                    end_datetime=end_value,
                    duration=duration_str,
                    bar_size=bar_size,
                    what_to_show='TRADES',
                    use_rth=False,
                    timeout=self._ibapi_historical_request_timeout,
                )
            except Exception as exc:
                ibkr_logger.error(
                    "Failed to request historical data via ibapi controller",  # noqa: TRY400
                    symbol=symbol,
                    error=str(exc),
                )
                return pd.DataFrame()

            if not historical_bars:
                ibkr_logger.warning(f"No historical data returned for {symbol}")
                return pd.DataFrame()

            data = []
            for bar in historical_bars:
                data.append(
                    {
                        'date': getattr(bar, 'date', None),
                        'open': getattr(bar, 'open', None),
                        'high': getattr(bar, 'high', None),
                        'low': getattr(bar, 'low', None),
                        'close': getattr(bar, 'close', None),
                        'volume': getattr(bar, 'volume', None),
                    }
                )

            df = pd.DataFrame(data)
            ibkr_logger.info(f"Retrieved {len(df)} historical data points for {symbol}")
            return df
        except IBKRConnectionError as exc:
            if self._should_log_historical_connection_loss(symbol):
                ibkr_logger.error(str(exc))
            return pd.DataFrame()
        except Exception as e:
            ibkr_logger.error(f"Failed to get historical data for {symbol}: {e}")
            return pd.DataFrame()

    async def _fetch_historical_ticks_ibapi(
        self,
        contract: IBAPIContract,
        *,
        start: str,
        end: str,
        count: int,
        what_to_show: str,
        use_rth: bool,
        ignore_size: bool,
        misc_options: Sequence[Any],
    ) -> List[Any]:
        controller = await self._ensure_ibapi_controller()
        if controller is None or not controller.is_connected():
            raise RuntimeError("ibapi controller unavailable")

        api_contract = self._convert_to_ibapi_contract(contract)
        ticks = await controller.request_historical_ticks(
            api_contract,
            start_datetime=start,
            end_datetime=end,
            number_of_ticks=count,
            what_to_show=what_to_show,
            use_rth=use_rth,
            ignore_size=ignore_size,
            misc_options=misc_options,
            timeout=self._ibapi_historical_request_timeout,
        )
        return list(ticks)

    # ------------------------------------------------------------------
    def get_dom_history(
        self,
        symbol: str,
        *,
        end_datetime: datetime | str | None = None,
        start_datetime: datetime | str | None = None,
        duration: str | None = None,
        depth: int = 10,
        exchange: str = "SMART",
        limit: int | None = None,
    ) -> list[dict[str, object]]:
        """Fetch historical DOM snapshots for *symbol* using historical ticks.

        Interactive Brokers does not provide full depth-of-book history; the
        closest available data comes from ``reqHistoricalTicks`` with
        ``whatToShow='BID_ASK'``.  Each returned snapshot therefore represents
        the top-of-book only.  The implementation now relies entirely on the
        in-tree :class:`IBAPIController`, so an active controller connection is
        required for the request to succeed.
        """

        symbol_key = (symbol or "").strip().upper()
        if not symbol_key:
            return []

        self._setup_async_components()

        loop = self._async_loop
        if loop is None or loop.is_closed():
            raise RuntimeError("IBKR async loop unavailable for DOM history requests")
        if not loop.is_running():
            raise RuntimeError("IBKR async loop not running for DOM history requests")

        def _await_coroutine(awaitable: Awaitable[Any], *, timeout: float) -> Any:
            future = asyncio.run_coroutine_threadsafe(awaitable, loop)
            try:
                return future.result(timeout=timeout)
            except Exception:
                future.cancel()
                raise

        try:
            controller = _await_coroutine(
                self._ensure_ibapi_controller(),
                timeout=self._ibapi_contract_connect_timeout,
            )
        except Exception as exc:
            raise RuntimeError(f"IBAPI controller unavailable for DOM history: {exc}") from exc

        if controller is None or not controller.is_connected():
            raise RuntimeError("IBAPI controller unavailable for DOM history requests")

        manager = self.connection_manager

        def _coerce_datetime(value: datetime | str | None) -> datetime | None:
            if value is None or value == "":
                return None
            if isinstance(value, datetime):
                dt = value
            else:
                text = str(value).strip()
                if not text:
                    return None
                if text.endswith("Z"):
                    text = text[:-1] + "+00:00"
                try:
                    dt = datetime.fromisoformat(text)
                except ValueError:
                    for fmt in ("%Y%m%d %H:%M:%S", "%Y%m%d"):
                        try:
                            dt = datetime.strptime(text, fmt)
                            break
                        except ValueError:
                            continue
                    else:
                        return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            return dt

        def _parse_duration(value: str | None) -> timedelta | None:
            if not value:
                return None
            text = value.strip().lower()
            if not text:
                return None
            condensed = text.replace(" ", "")
            match = re.match(r"^(?P<count>\d+(?:\.\d+)?)(?P<unit>[a-z]+)$", condensed)
            if match is None:
                parts = text.split()
                if len(parts) == 2:
                    try:
                        count = float(parts[0])
                    except (TypeError, ValueError):
                        return None
                    unit = parts[1]
                else:
                    return None
            else:
                try:
                    count = float(match.group("count"))
                except (TypeError, ValueError):
                    return None
                unit = match.group("unit")

            unit_seconds = {
                "s": 1,
                "sec": 1,
                "secs": 1,
                "second": 1,
                "seconds": 1,
                "m": 60,
                "min": 60,
                "mins": 60,
                "minute": 60,
                "minutes": 60,
                "h": 3600,
                "hr": 3600,
                "hrs": 3600,
                "hour": 3600,
                "hours": 3600,
                "d": 86400,
                "day": 86400,
                "days": 86400,
                "w": 604800,
                "week": 604800,
                "weeks": 604800,
                "mon": 2628000,
                "month": 2628000,
                "months": 2628000,
                "y": 31536000,
                "yr": 31536000,
                "yrs": 31536000,
                "year": 31536000,
                "years": 31536000,
            }.get(unit)

            if unit_seconds is None:
                return None
            return timedelta(seconds=count * unit_seconds)

        def _format_ib_datetime(value: datetime | None) -> str:
            if value is None:
                return ""
            return value.astimezone(timezone.utc).strftime("%Y%m%d %H:%M:%S")

        def _coerce_tick_time(value: Any) -> datetime | None:
            if isinstance(value, datetime):
                dt = value
            elif isinstance(value, (int, float)):
                try:
                    dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
                except Exception:
                    return None
            else:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            return dt

        end_dt = _coerce_datetime(end_datetime) or datetime.now(timezone.utc)
        start_dt = _coerce_datetime(start_datetime)
        if start_dt is None and duration:
            duration_delta = _parse_duration(duration)
            if duration_delta:
                start_dt = end_dt - duration_delta
        if start_dt is not None and start_dt >= end_dt:
            start_dt = end_dt - timedelta(seconds=1)

        self._setup_async_components()

        try:
            limit_value = int(limit) if limit is not None else 0
        except (TypeError, ValueError):
            limit_value = 0
        limit_value = max(0, limit_value)
        total_remaining = limit_value or 1000
        max_per_request = 1000

        sec_type = "FUT" if symbol_key in INDEX_FUTURE_SYMBOLS else "STK"
        contract = self.get_contract(symbol_key, sec_type=sec_type, exchange=exchange)

        try:
            qualified_contract = _await_coroutine(
                self._qualify_contract(
                    contract,
                    symbol=symbol_key,
                    exchange=getattr(contract, "exchange", exchange) or exchange,
                    currency=getattr(contract, "currency", "USD") or "USD",
                ),
                timeout=self._ibapi_contract_request_timeout,
            )
        except Exception as exc:
            raise RuntimeError(f"Failed to qualify contract for DOM history: {exc}") from exc

        if not isinstance(qualified_contract, IBAPIContract):
            qualified_contract = self._convert_to_ibapi_contract(qualified_contract)

        if qualified_contract is None:
            raise RuntimeError("Unable to qualify contract for DOM history")

        collected: list[Any] = []
        current_end = end_dt
        iterations = 0
        while total_remaining > 0:
            iterations += 1
            batch_size = min(total_remaining, max_per_request)
            start_str = _format_ib_datetime(start_dt)
            end_str = _format_ib_datetime(current_end)

            def _request() -> list[Any]:
                try:
                    result = _await_coroutine(
                        self._fetch_historical_ticks_ibapi(
                            qualified_contract,
                            start=start_str,
                            end=end_str,
                            count=batch_size,
                            what_to_show="BID_ASK",
                            use_rth=False,
                            ignore_size=False,
                            misc_options=[],
                        ),
                        timeout=self._ibapi_historical_request_timeout + 1.0,
                    )
                except Exception as exc:
                    raise RuntimeError(
                        f"IBAPI historical tick request failed: {exc}"
                    ) from exc

                return list(result or [])

            try:
                batch = manager.run_in_connection_thread(_request, timeout=30.0)
            except Exception as exc:
                raise RuntimeError(f"IBKR historical DOM request failed: {exc}") from exc

            if not batch:
                break

            collected.extend(batch)
            total_remaining -= len(batch)

            if start_dt is None:
                break

            earliest = None
            for item in batch:
                ts = _coerce_tick_time(getattr(item, "time", None))
                if ts is None:
                    continue
                earliest = ts if earliest is None else min(earliest, ts)
            if earliest is None:
                break
            if earliest <= start_dt or len(batch) < batch_size:
                break

            current_end = earliest - timedelta(milliseconds=1)
            if iterations > 20:
                break

        if not collected:
            return []

        def _build_levels(price: Any, size: Any) -> list[dict[str, float]]:
            price_value = self._sanitize_float(price)
            size_value = self._sanitize_float(size)
            if price_value is None:
                return []
            return [
                {
                    "level": 0,
                    "price": price_value,
                    "size": size_value if size_value is not None else 0.0,
                }
            ]

        snapshots: list[dict[str, object]] = []
        seen_ns: set[int] = set()
        collected.sort(key=lambda item: _coerce_tick_time(getattr(item, "time", None)) or datetime.min)
        for entry in collected:
            ts = _coerce_tick_time(getattr(entry, "time", None))
            if ts is None:
                continue
            ts_ns = int(ts.timestamp() * 1_000_000_000)
            if ts_ns in seen_ns:
                continue
            seen_ns.add(ts_ns)
            bids = _build_levels(getattr(entry, "priceBid", None), getattr(entry, "sizeBid", None))
            asks = _build_levels(getattr(entry, "priceAsk", None), getattr(entry, "sizeAsk", None))
            snapshots.append(
                {
                    "symbol": symbol_key,
                    "timestamp": ts.isoformat(),
                    "timestamp_ns": ts_ns,
                    "bids": bids,
                    "asks": asks,
                }
            )

        if limit_value and len(snapshots) > limit_value:
            snapshots = snapshots[-limit_value:]

        return snapshots

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
        """Push historical bars to the market data service for distribution."""

        if df is None or df.empty:
            ibkr_logger.debug(f"Skipping historical data publish for {symbol}: no rows")
            return

        try:
            # Include the index as a column so downstream consumers receive timestamps.
            reset_df = df.reset_index(drop=False)
        except Exception:
            reset_df = df

        try:
            records = reset_df.to_dict("records")
        except Exception as exc:
            ibkr_logger.error(
                f"Failed to serialise historical data for {symbol}: {exc}"
            )
            return

        if not records:
            ibkr_logger.debug(
                f"Skipping historical data publish for {symbol}: serialised payload empty"
            )
            return

        def _normalise_value(value: Any) -> Any:
            if pd.isna(value):  # type: ignore[arg-type]
                return None
            if isinstance(value, pd.Timestamp):
                value = value.to_pydatetime()
            if isinstance(value, datetime):
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                return value.isoformat()
            return value

        bars: List[Dict[str, Any]] = []
        for row in records:
            normalised_row = {key: _normalise_value(val) for key, val in row.items()}
            bars.append(normalised_row)

        payload: Dict[str, Any] = {
            "symbol": symbol,
            "bars": bars,
        }

        # Preserve optional metadata for observability (non-breaking if filename unused).
        metadata: Dict[str, Any] = {
            "row_count": len(bars),
        }
        if filename:
            metadata["source_filename"] = filename
        if hasattr(df, "attrs"):
            bar_size = df.attrs.get("bar_size")
            if bar_size:
                metadata["bar_size"] = bar_size
        payload["metadata"] = metadata

        async def _push_to_service() -> None:
            client = _get_market_data_service_client()
            try:
                await client.request("POST", _MARKET_DATA_INGEST_PATH, json=payload)
                ibkr_logger.info(
                    f"Published {len(bars)} historical bars for {symbol} to market data service"
                )
            except ServiceUnavailableError as exc:
                ibkr_logger.warning(
                    f"Market data service unavailable when publishing {symbol} bars: {exc}"
                )
            except ServiceResponseError as exc:
                ibkr_logger.warning(
                    "Market data service rejected {} bars with status {}: {}",
                    symbol,
                    exc.status_code,
                    exc.detail,
                )
            except ServiceClientError as exc:
                ibkr_logger.warning(
                    f"Failed to deliver {symbol} bars to market data service: {exc}"
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                ibkr_logger.exception(
                    f"Unexpected error publishing {symbol} historical data: {exc}"
                )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(_push_to_service())
        else:
            task = loop.create_task(_push_to_service())

            def _log_task_result(fut: asyncio.Future[Any]) -> None:
                try:
                    fut.result()
                except Exception as exc:  # pragma: no cover - defensive guard
                    ibkr_logger.exception(
                        f"Background publish of {symbol} bars failed: {exc}"
                    )

            task.add_done_callback(_log_task_result)
    
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
        try:
            symbol_upper = (symbol or '').upper()
            is_index_future = symbol_upper in INDEX_FUTURE_SYMBOLS

            cached_time = self.price_cache_timestamp.get(symbol)
            if symbol in self.price_cache and cached_time:
                if (now_local() - cached_time).total_seconds() < self.price_cache_ttl_seconds:
                    ibkr_logger.debug(f"Returning cached data for {symbol}")
                    return {
                        'symbol': symbol,
                        'last': self._sanitize_float(self.price_cache[symbol]),
                        'bid': 0.0,
                        'ask': 0.0,
                        'volume': 0.0,
                        'timestamp': to_local_time(datetime.now(NY_TZ)),
                        'is_cached': True,
                    }

            controller = await self._ensure_ibapi_controller()
            if controller is None or not controller.is_connected():
                ibkr_logger.warning(
                    "IBAPI controller unavailable for real-time data",
                    symbol=symbol,
                )
                return None

            router = await self._ensure_market_data_router()
            if router is None:
                ibkr_logger.error("Market data router unavailable; cannot stream {}", symbol)
                return None

            event = asyncio.Event()
            received: Dict[str, Mapping[str, Any]] = {}

            def _capture(sym: str, payload: Mapping[str, Any]) -> None:
                if sym.upper() != symbol_upper:
                    return
                received['payload'] = payload
                if not event.is_set():
                    event.set()

            with self._market_data_lock:
                self._price_listeners.append(_capture)

            added_subscription = False
            try:
                with self._market_data_lock:
                    already_tracked = symbol_upper in self._market_data_router_symbols

                options: Dict[str, Any] = {"exchange": exchange}
                if is_index_future:
                    options["sec_type"] = "FUT"

                await self.subscribe_market_data(symbol, options=options)

                with self._market_data_lock:
                    if symbol_upper in self._market_data_router_symbols and not already_tracked:
                        added_subscription = True

                max_wait = 3.0 if is_index_future else 1.0
                try:
                    await asyncio.wait_for(event.wait(), timeout=max_wait)
                except asyncio.TimeoutError:
                    ibkr_logger.warning(
                        "No ibapi market data received for {} within {}s",
                        symbol,
                        max_wait,
                    )

                payload = received.get('payload')
                if not payload:
                    if symbol in self.price_cache:
                        cached_price = self.price_cache[symbol]
                        ibkr_logger.info(
                            "Returning cached price for {} after ibapi timeout: {}",
                            symbol,
                            cached_price,
                        )
                        return {
                            'symbol': symbol,
                            'last': self._sanitize_float(cached_price),
                            'bid': 0.0,
                            'ask': 0.0,
                            'volume': 0.0,
                            'timestamp': to_local_time(datetime.now(NY_TZ)),
                            'is_cached': True,
                        }
                    return None

                last = self._sanitize_float(payload.get('last'))
                bid = self._sanitize_float(payload.get('bid'))
                ask = self._sanitize_float(payload.get('ask'))
                volume = self._sanitize_float(payload.get('volume'))

                price = last
                if price <= 0.0 and bid > 0.0 and ask > 0.0:
                    price = (bid + ask) / 2.0

                if price > 0.0:
                    self.price_cache[symbol] = price
                    self.price_cache_timestamp[symbol] = now_local()

                timestamp = payload.get('timestamp')
                try:
                    timestamp_text = to_local_time(timestamp)
                except Exception:
                    timestamp_text = to_local_time(datetime.now(NY_TZ))

                return {
                    'symbol': symbol,
                    'last': self._sanitize_float(price),
                    'bid': bid,
                    'ask': ask,
                    'volume': volume,
                    'timestamp': timestamp_text,
                }
            finally:
                with self._market_data_lock:
                    try:
                        self._price_listeners.remove(_capture)
                    except ValueError:
                        pass

                if added_subscription:
                    try:
                        await self.unsubscribe_market_data(symbol)
                    except Exception as exc:  # pragma: no cover - defensive guard
                        ibkr_logger.debug(
                            "Failed to clean up transient market data subscription",  # noqa: TRY400
                            symbol=symbol,
                            error=str(exc),
                        )
        except Exception as exc:
            ibkr_logger.error(
                "Exception in ibapi real-time data path for {}: {}",
                symbol,
                exc,
            )
            if symbol in self.price_cache:
                cached_price = self.price_cache[symbol]
                return {
                    'symbol': symbol,
                    'last': self._sanitize_float(cached_price),
                    'bid': 0.0,
                    'ask': 0.0,
                    'volume': 0.0,
                    'timestamp': to_local_time(datetime.now(NY_TZ)),
                    'is_cached': True,
                    'error': str(exc),
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

    async def _release_ibapi_market_data(self) -> None:
        router = self._market_data_router
        if router is None:
            return

        with self._market_data_lock:
            symbols = list(self._market_data_router_symbols)
            self._market_data_router_symbols.clear()

        for symbol in symbols:
            try:
                await router.unsubscribe(symbol)
            except Exception as exc:  # pragma: no cover - defensive guard
                ibkr_logger.debug(
                    "Failed to unsubscribe ibapi market data for {}: {}",
                    symbol,
                    exc,
                )

    # ------------------------------------------------------------------
    async def subscribe_market_data(
        self,
        symbol: str,
        *,
        options: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Subscribe to IBKR market data and forward updates to listeners."""

        symbol_key = (symbol or "").strip().upper()
        if not symbol_key:
            raise ValueError("Symbol is required for market data subscription")

        await self.ensure_connection(reason=f"market-data:{symbol_key}")

        options = dict(options or {})
        dom_rows = options.pop("dom_rows", self._default_dom_rows)
        try:
            dom_rows = int(dom_rows)
        except (TypeError, ValueError):
            dom_rows = self._default_dom_rows
        dom_rows = max(0, min(dom_rows, 40))

        with self._market_data_lock:
            if symbol_key in self._market_data_router_symbols or symbol_key in self._market_data_streams:
                return

        router = await self._ensure_market_data_router()
        if router is None:
            raise RuntimeError("IBAPI market data router unavailable")

        contract = self._resolve_market_data_contract(symbol_key, options)
        exchange = (getattr(contract, "exchange", None) or options.get("exchange") or "SMART").upper()
        currency = getattr(contract, "currency", "USD") or "USD"

        qualified = await self._qualify_contract(
            contract,
            symbol=symbol_key,
            exchange=exchange,
            currency=currency,
        )
        if qualified is not None:
            contract = qualified

        api_contract = self._convert_to_ibapi_contract(contract)

        try:
            await router.subscribe(
                symbol=symbol_key,
                contract=api_contract,
                generic_tick_list=str(options.get("generic_tick_list", "")),
                snapshot=bool(options.get("snapshot", False)),
                regulatory_snapshot=bool(options.get("regulatory_snapshot", False)),
                options=None,
                dom_rows=dom_rows,
                is_smart_depth=bool(options.get("is_smart_depth", False)),
            )
        except Exception as exc:
            raise RuntimeError(
                f"Failed to subscribe via ibapi controller for {symbol_key}: {exc}"
            ) from exc

        with self._market_data_lock:
            self._market_data_router_symbols.add(symbol_key)

    async def _subscribe_market_data_legacy(
        self,
        symbol_key: str,
        *,
        options: Dict[str, Any],
        dom_rows: int,
        timeout: float,
    ) -> None:
        def _start_subscription():
            cm = self.connection_manager
            ib = cm.get_ib_instance()
            if not ib:
                raise RuntimeError("IBKR connection unavailable when subscribing market data")
            contract = self._resolve_market_data_contract(symbol_key, options)
            price_ticker = ib.reqMktData(contract, "", False, False)
            dom_ticker = None
            if dom_rows > 0:
                dom_ticker = ib.reqMktDepth(contract, numRows=dom_rows, isSmartDepth=False)
            return contract, price_ticker, dom_ticker

        result = await asyncio.to_thread(
            self.connection_manager.run_in_connection_thread,
            _start_subscription,
            timeout=timeout,
        )

        if not result:
            raise RuntimeError(f"Failed to subscribe market data for {symbol_key}")

        contract, price_ticker, dom_ticker = result

        def _on_price_update(*_args, sym: str = symbol_key, ticker=price_ticker) -> None:
            payload = self._build_price_payload(sym, ticker)
            if payload is None:
                return
            self._notify_price_listeners(sym, payload)

        def _on_dom_update(*_args, sym: str = symbol_key, ticker=dom_ticker) -> None:
            if ticker is None:
                return
            levels = self._build_dom_levels(sym, ticker)
            if not levels:
                return
            self._notify_dom_listeners(sym, levels)

        try:
            price_ticker.updateEvent += _on_price_update
        except Exception as exc:
            raise RuntimeError(f"Failed to attach price listener for {symbol_key}: {exc}") from exc

        if dom_ticker is not None:
            try:
                dom_ticker.updateEvent += _on_dom_update
            except Exception as exc:
                try:
                    price_ticker.updateEvent -= _on_price_update
                except Exception:
                    pass
                raise RuntimeError(f"Failed to attach DOM listener for {symbol_key}: {exc}") from exc

        subscription = _MarketDataSubscription(
            symbol=symbol_key,
            contract=contract,
            price_ticker=price_ticker,
            dom_ticker=dom_ticker,
            price_handler=_on_price_update,
            dom_handler=_on_dom_update if dom_ticker is not None else None,
            options=options,
            dom_rows=dom_rows,
        )

        with self._market_data_lock:
            self._market_data_streams[symbol_key] = subscription

        initial_payload = self._build_price_payload(symbol_key, price_ticker)
        if initial_payload is not None:
            self._notify_price_listeners(symbol_key, initial_payload)
        if dom_ticker is not None:
            initial_levels = self._build_dom_levels(symbol_key, dom_ticker)
            if initial_levels:
                self._notify_dom_listeners(symbol_key, initial_levels)

    async def unsubscribe_market_data(self, symbol: str) -> None:
        """Cancel a live market data subscription."""

        symbol_key = (symbol or "").strip().upper()
        if not symbol_key:
            return

        with self._market_data_lock:
            if symbol_key in self._market_data_router_symbols:
                self._market_data_router_symbols.remove(symbol_key)
                subscription = None
                router_symbol = True
            else:
                subscription = self._market_data_streams.pop(symbol_key, None)
                router_symbol = False

        if router_symbol:
            router = self._market_data_router
            if router is not None:
                try:
                    await router.unsubscribe(symbol_key)
                except Exception as exc:  # pragma: no cover - defensive guard
                    ibkr_logger.debug(
                        "Failed to unsubscribe ibapi market data", symbol=symbol_key, error=str(exc)
                    )
            return

        if subscription is None:
            return

        self._detach_subscription_handlers(subscription)

        def _cancel_subscription(sub: _MarketDataSubscription) -> None:
            cm = self.connection_manager
            ib = cm.get_ib_instance()
            if not ib:
                return
            try:
                ib.cancelMktData(sub.contract)
            except Exception:
                pass
            if sub.dom_ticker is not None:
                try:
                    ib.cancelMktDepth(sub.contract)
                except Exception:
                    pass

        await asyncio.to_thread(
            self.connection_manager.run_in_connection_thread,
            _cancel_subscription,
            subscription,
            timeout=5.0,
        )


    async def place_market_order(
        self,
        symbol: str,
        quantity: int,
        action: str = 'BUY',
        exchange: str = 'SMART',
        sec_type: str = 'STK',
    ) -> Optional[Dict[str, Any]]:
        """下市价单"""
        # 下单前备份订单信息
        # self._backup_order(symbol, quantity, action, 'MARKET', sec_type=sec_type, exchange=exchange)

        try:
            trade = await self._place_order_via_ibapi_controller(
                symbol=symbol,
                quantity=quantity,
                action=action,
                exchange=exchange,
                sec_type=sec_type,
                order_type='MARKET',
                limit_price=None,
            )
        except Exception as exc:
            ibkr_logger.error("Failed to place market order via controller: {}", exc)
            return None

        if trade is None:
            ibkr_logger.error("IBAPI controller returned no trade information for market order")
            return None

        return trade_to_dict(
            trade,
            defaults={
                "symbol": symbol,
                "action": action,
                "quantity": float(quantity),
                "order_type": 'MARKET',
                "exchange": exchange,
                "sec_type": sec_type.upper() if isinstance(sec_type, str) else sec_type,
            },
        )

    async def place_limit_order(
        self,
        symbol: str,
        quantity: int,
        price: float,
        action: str = 'BUY',
        exchange: str = 'SMART',
        sec_type: str = 'STK',
    ) -> Optional[Dict[str, Any]]:
        """下限价单"""
        # 下单前备份订单信息
        # self._backup_order(symbol, quantity, action, 'LIMIT', price, sec_type=sec_type, exchange=exchange)

        try:
            trade = await self._place_order_via_ibapi_controller(
                symbol=symbol,
                quantity=quantity,
                action=action,
                exchange=exchange,
                sec_type=sec_type,
                order_type='LIMIT',
                limit_price=price,
            )
        except Exception as exc:
            ibkr_logger.error("Failed to place limit order via controller: {}", exc)
            return None

        if trade is None:
            ibkr_logger.error("IBAPI controller returned no trade information for limit order")
            return None

        return trade_to_dict(
            trade,
            defaults={
                "symbol": symbol,
                "action": action,
                "quantity": float(quantity),
                "order_type": 'LIMIT',
                "limit_price": float(price),
                "exchange": exchange,
                "sec_type": sec_type.upper() if isinstance(sec_type, str) else sec_type,
            },
        )


    async def cancel_order(self, order_id: Any) -> tuple[bool, str]:
        """取消指定订单"""
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

        controller = await self._ensure_ibapi_controller()
        if controller is None or not controller.is_connected():
            ibkr_logger.error("Cannot cancel order %s: IBAPI controller unavailable", order_id_str)
            return False, "Controller unavailable"

        try:
            status = await controller.cancel_order(
                order_id_int,
                timeout=self._ibapi_order_cancel_timeout,
            )
        except Exception as exc:
            ibkr_logger.error("Failed to cancel order %s via controller: %s", order_id_str, exc)
            return False, f"Cancel failed: {exc}"

        message = f"Order cancelled successfully, status: {status}" if status else "Order cancelled via ibapi"
        return True, message



    async def cancel_all_orders(self) -> int:
        """取消所有订单"""

        if is_trading_disabled():
            ibkr_logger.warning("Trading disabled; skipping cancel_all_orders request")
            return 0

        controller = await self._ensure_ibapi_controller()
        if controller is None or not controller.is_connected():
            ibkr_logger.error("Cannot cancel all orders: IBAPI controller unavailable")
            return 0

        order_ids = self._trade_book.cancellable_order_ids()
        if not order_ids:
            ibkr_logger.info("No cancellable orders found via trade book snapshot")
            return 0

        cancelled = 0
        for order_id in order_ids:
            try:
                await controller.cancel_order(
                    order_id,
                    timeout=self._ibapi_order_cancel_timeout,
                )
            except Exception as exc:
                ibkr_logger.warning("Failed to cancel order %s: %s", order_id, exc)
            else:
                cancelled += 1

        ibkr_logger.info("Submitted cancellation for %d/%d orders", cancelled, len(order_ids))

        try:
            await self._trade_book.refresh()
        except Exception:
            pass

        return cancelled



    def get_orders(self, risk_manager=None, strategy_manager=None) -> List[Dict]:
        """获取订单列表（支持去抖动，避免重复刷新）。"""
        controller = self._ibapi_controller
        controller_connected = controller is not None and controller.is_connected()

        try:
            connection_active = self.connection_manager.is_connected()
        except Exception:
            connection_active = False

        try:
            if not (connection_active or controller_connected):
                ibkr_logger.error("Cannot retrieve orders: IBKR is not connected")
                return []

            snapshots = self._trade_book.snapshot()
            if not snapshots:
                loop = self._get_async_loop()
                controller = self._ibapi_controller
                if loop and controller and controller.is_connected():
                    try:
                        future = asyncio.run_coroutine_threadsafe(
                            self._trade_book.refresh(),
                            loop,
                        )
                        future.result(timeout=2.0)
                    except Exception:
                        pass
                    snapshots = self._trade_book.snapshot()

            orders: List[Dict[str, Any]] = []
            auto_orders = getattr(risk_manager, "auto_orders", {}) if risk_manager else {}

            for record in snapshots:
                order = getattr(record, "order", None)
                if order is None:
                    continue

                order_id = getattr(order, "orderId", None)
                if not order_id:
                    continue

                quantity = self._sanitize_float(getattr(order, "totalQuantity", None)) or 0.0
                filled = self._sanitize_float(record.filled) or 0.0
                remaining_qty = max(0.0, quantity - filled)
                status_text = str(getattr(record, "status", "") or "")

                if remaining_qty <= 0 and status_text.upper() in _FINAL_ORDER_STATES:
                    continue

                contract = getattr(record, "contract", None)
                symbol = None
                if contract is not None:
                    symbol = getattr(contract, "localSymbol", None) or getattr(contract, "symbol", None)

                order_id_str = str(order_id)
                auto_order = auto_orders.get(order_id_str) if auto_orders else None
                raw_source = getattr(auto_order, "order_source", None) if auto_order else None
                source_code = raw_source if raw_source in {"manual", "auto", "strategy", "tp", "sl", "bot"} else "manual"
                source_labels = {
                    "manual": "人工",
                    "auto": "机器人",
                    "strategy": "策略",
                    "tp": "tp",
                    "sl": "sl",
                    "bot": "bot",
                }
                order_source = source_labels.get(source_code, source_code)

                if auto_order is not None:
                    current_status = status_text
                    current_avg_fill_price = self._sanitize_float(record.avg_fill_price)
                    if getattr(auto_order, "status", None) != current_status:
                        auto_order.status = current_status
                    if current_avg_fill_price and current_avg_fill_price > 0:
                        if getattr(auto_order, "price", None) != current_avg_fill_price:
                            auto_order.price = current_avg_fill_price
                    if (
                        current_status and current_status.upper() == "FILLED"
                        and getattr(auto_order, "executed_at", None) is None
                    ):
                        auto_order.executed_at = now_local()

                orders.append(
                    {
                        "order_id": order_id_str,
                        "symbol": symbol,
                        "action": getattr(order, "action", None),
                        "quantity": quantity,
                        "order_type": getattr(order, "orderType", None),
                        "limit_price": self._sanitize_float(getattr(order, "lmtPrice", None)),
                        "status": status_text,
                        "filled": filled,
                        "remaining": remaining_qty,
                        "avg_fill_price": self._sanitize_float(record.avg_fill_price),
                        "submit_time": record.submitted_at.isoformat() if getattr(record, "submitted_at", None) else None,
                        "order_source": order_source,
                    }
                )

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

            controller_snapshot = await self._request_account_updates_via_controller()
            if not controller_snapshot:
                ibkr_logger.warning(
                    "IBAPI controller returned no account snapshot; using cached account info"
                )
                return self._return_cached_account_info()

            account_values = controller_snapshot.get("account_values") or []
            if not account_values:
                ibkr_logger.warning("No account values returned; using cached snapshot if available")
                return self._return_cached_account_info()

            # Start from the last cached snapshot when available so that we don't
            # accidentally wipe fields to zero if IBKR returns a partial update.
            info = self._return_cached_account_info() or {}
            info = dict(info)  # ensure we don't mutate the cached reference

            # Guarantee all expected keys exist even when we start from a cached
            # snapshot created by an earlier version of the code.
            info.setdefault('account_id', None)
            info['account_type'] = (
                'Paper' if self.connection_manager.config.paper_trading else 'Live'
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

            def _extract(item: Any, key: str) -> Any:
                if isinstance(item, dict):
                    return item.get(key)
                return getattr(item, key, None)

            for item in account_values:
                tag = _extract(item, 'tag') or ''
                value = _extract(item, 'value')
                currency = (_extract(item, 'currency') or '').upper()
                account = _extract(item, 'account')

                # Account id may be stored either in AccountCode value or the account field itself
                if tag == 'AccountCode' and value:
                    info['account_id'] = value
                elif not info['account_id'] and account:
                    info['account_id'] = account

                # IBKR may report base currency values using either USD or BASE depending on
                # account configuration, so accept both to avoid zeroing out valid data.
                currency_ok = currency in ('USD', 'BASE', '')

                if tag == 'TotalCashValue' and currency_ok:
                    info['total_cash'] = _parse_float(value)
                elif tag == 'AvailableFunds' and currency_ok:
                    info['available_funds'] = _parse_float(value)
                elif tag == 'BuyingPower' and currency_ok:
                    info['buying_power'] = _parse_float(value)
                elif tag == 'NetLiquidation' and currency_ok:
                    info['net_liquidation'] = _parse_float(value)
                elif tag == 'UnrealizedPnL' and currency_ok:
                    info['unrealized_pnl'] = _parse_float(value)
                elif tag == 'RealizedPnL' and currency_ok:
                    info['realized_pnl'] = _parse_float(value)

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

            controller_summary = await self._request_account_summary_via_controller()
            if not controller_summary:
                ibkr_logger.warning("IBAPI controller returned no account summary; returning empty list")
                return []

            result: List[Dict] = []

            def _extract(item: Any, key: str) -> Any:
                if isinstance(item, dict):
                    return item.get(key)
                return getattr(item, key, None)

            for it in controller_summary:
                try:
                    result.append(
                        {
                            'tag': _extract(it, 'tag'),
                            'value': _extract(it, 'value'),
                            'currency': _extract(it, 'currency'),
                            'account': _extract(it, 'account'),
                        }
                    )
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

            controller_snapshot = await self._request_account_updates_via_controller()
            positions_snapshot = await self._request_positions_via_controller()

            portfolio_items: List[SimpleNamespace] = []
            portfolio_map: Dict[Tuple[Any, ...], SimpleNamespace] = {}

            def _extract(item: Any, key: str) -> Any:
                if isinstance(item, dict):
                    return item.get(key)
                return getattr(item, key, None)

            if controller_snapshot:
                for raw in controller_snapshot.get("portfolio", []) or []:
                    contract = _extract(raw, "contract")
                    if contract is None:
                        continue
                    key = self._contract_cache_key(contract)
                    entry = SimpleNamespace(
                        contract=contract,
                        position=float(_extract(raw, "position") or 0.0),
                        marketPrice=float(_extract(raw, "market_price") or 0.0),
                        marketValue=float(_extract(raw, "market_value") or 0.0),
                        averageCost=float(_extract(raw, "average_cost") or 0.0),
                        unrealizedPNL=float(_extract(raw, "unrealized_pnl") or 0.0),
                        realizedPNL=float(_extract(raw, "realized_pnl") or 0.0),
                        account=_extract(raw, "account"),
                    )
                    portfolio_map[key] = entry

            if positions_snapshot:
                for raw in positions_snapshot:
                    contract = _extract(raw, "contract")
                    if contract is None:
                        continue
                    key = self._contract_cache_key(contract)
                    entry = portfolio_map.get(key)
                    position = float(_extract(raw, "position") or 0.0)
                    avg_cost = float(_extract(raw, "avg_cost") or 0.0)
                    account = _extract(raw, "account")

                    if entry is None:
                        entry = SimpleNamespace(
                            contract=contract,
                            position=position,
                            marketPrice=0.0,
                            marketValue=0.0,
                            averageCost=avg_cost,
                            unrealizedPNL=0.0,
                            realizedPNL=0.0,
                            account=account,
                        )
                        portfolio_map[key] = entry
                    else:
                        entry.contract = contract
                        entry.position = position
                        if not getattr(entry, "account", None) and account:
                            entry.account = account
                        if avg_cost and not getattr(entry, "averageCost", 0.0):
                            entry.averageCost = avg_cost

            portfolio_items = list(portfolio_map.values())

            # 当发生短暂断线/同步延迟时，控制器可能返回空的快照。
            # 为避免前端仓位瞬时清空，这里使用短期缓存回退。
            if not portfolio_items:
                if getattr(self, 'positions_cache', None) and getattr(self, 'positions_cache_timestamp', None) and (now_local() - self.positions_cache_timestamp) < timedelta(seconds=self.positions_cache_ttl_seconds):
                    ibkr_logger.warning("IBAPI controller returned empty positions; using recent cache to avoid flicker")
                    return self.positions_cache.copy()
                else:
                    ibkr_logger.warning("IBAPI controller returned empty positions and no valid cache; returning empty list")
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
            connection_logger.error(
                "Order error - ReqId: {}, Code: {}, Message: {}",
                req_id,
                error_code,
                error_string,
            )

            try:
                order_id_int = int(req_id)
            except Exception:
                order_id_int = None

            if order_id_int is not None:
                self._trade_book.mark_error(order_id_int, f"Error {error_code}")
            else:
                connection_logger.warning("Unable to determine order id for reqId {}", req_id)

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
        """更新订单的佣金和PnL信息并在服务不可用时缓存结果。"""

        oid = str(order_id)
        payload = {"commission": commission, "realized_pnl": pnl}
        cache_entry = dict(payload)
        cache_entry["pending_service_sync"] = False
        self._order_fill_cache[oid] = cache_entry

        client = getattr(self, "_orders_service_client", None)
        if client is None:
            client = _get_orders_service_client()
            try:
                self._orders_service_client = client
            except AttributeError:  # pragma: no cover - instance bypassed __init__
                pass
        try:
            await client.update_order_fill_metrics(oid, payload)
        except ServiceUnavailableError as exc:
            cache_entry["pending_service_sync"] = True
            connection_logger.warning(
                "Orders service unavailable for commission update; cached locally",
                order_id=oid,
                error=str(exc),
            )
        except ServiceResponseError as exc:
            cache_entry["pending_service_sync"] = True
            detail = exc.detail or ""
            connection_logger.warning(
                "Orders service rejected commission update; cached locally",
                order_id=oid,
                detail=detail,
            )
        except ServiceClientError as exc:
            cache_entry["pending_service_sync"] = True
            connection_logger.warning(
                "Failed to update order commission/pnl via service; cached locally",
                order_id=oid,
                error=str(exc),
            )
        else:
            self._order_fill_cache.pop(oid, None)
            connection_logger.info(
                "Updated order {} commission={}, pnl={} via orders service",
                oid,
                commission,
                pnl,
            )
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

    # ------------------------------------------------------------------
    def _resolve_market_data_contract(self, symbol: str, options: Dict[str, Any]):
        exchange = (options.get("exchange") or "SMART").upper()
        sec_type = (options.get("sec_type") or "").upper()
        if sec_type == "FUT" or symbol.upper() in INDEX_FUTURE_SYMBOLS:
            return self.get_future_contract(symbol, exchange)
        return self.get_stock_contract(symbol, exchange)

    def _build_price_payload(self, symbol: str, ticker: Any) -> Optional[Dict[str, Any]]:
        try:
            bid = self._coerce_float(getattr(ticker, "bid", 0.0))
            ask = self._coerce_float(getattr(ticker, "ask", 0.0))
            last = self._coerce_float(getattr(ticker, "last", 0.0))
            volume = self._coerce_float(getattr(ticker, "volume", 0.0))
            bid_size = self._coerce_float(getattr(ticker, "bidSize", getattr(ticker, "bid_size", 0.0)))
            ask_size = self._coerce_float(getattr(ticker, "askSize", getattr(ticker, "ask_size", 0.0)))
            market_state = getattr(ticker, "marketState", None)
            timestamp = getattr(ticker, "time", None)
        except Exception as exc:  # pragma: no cover - defensive guard
            ibkr_logger.debug("Failed to read price ticker fields", symbol=symbol, error=str(exc))
            return None

        if (bid or ask or last) is None:
            return None

        if timestamp is None:
            timestamp = datetime.now(timezone.utc)
        elif isinstance(timestamp, datetime) and timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)

        payload: Dict[str, Any] = {
            "symbol": symbol,
            "bid": bid or 0.0,
            "ask": ask or 0.0,
            "last": last or 0.0,
            "bid_size": bid_size or 0.0,
            "ask_size": ask_size or 0.0,
            "volume": volume or 0.0,
            "market_state": market_state or "unknown",
            "timestamp": timestamp,
        }
        return payload

    def _build_dom_levels(self, symbol: str, ticker: Any) -> List[Dict[str, Any]]:
        levels: List[Dict[str, Any]] = []
        try:
            bids = list(getattr(ticker, "domBids", []) or [])
            asks = list(getattr(ticker, "domAsks", []) or [])
        except Exception as exc:  # pragma: no cover - defensive guard
            ibkr_logger.debug("Failed to fetch DOM levels", symbol=symbol, error=str(exc))
            return levels

        def _process(entries: Sequence[Any], side: str) -> None:
            for idx, entry in enumerate(entries):
                try:
                    price = self._coerce_float(getattr(entry, "price", 0.0)) or 0.0
                    size = self._coerce_float(getattr(entry, "size", 0.0)) or 0.0
                    level = getattr(entry, "position", idx)
                    try:
                        level = int(level)
                    except Exception:
                        level = idx
                except Exception as exc:  # pragma: no cover - defensive guard
                    ibkr_logger.debug("Invalid DOM entry", symbol=symbol, side=side, error=str(exc))
                    continue
                levels.append(
                    {
                        "symbol": symbol,
                        "side": side,
                        "level": level,
                        "price": price,
                        "size": size,
                        "timestamp": datetime.now(timezone.utc),
                    }
                )

        _process(bids, "bid")
        _process(asks, "ask")

        return levels

    def _coerce_float(self, value: Any) -> float:
        try:
            return float(value or 0.0)
        except Exception:
            return 0.0

    def _notify_price_listeners(self, symbol: str, payload: Mapping[str, Any]) -> None:
        listeners: List[Callable[[str, Mapping[str, Any]], None]]
        with self._market_data_lock:
            listeners = list(self._price_listeners)
        for callback in listeners:
            try:
                callback(symbol, payload)
            except Exception as exc:  # pragma: no cover - defensive guard
                ibkr_logger.debug("Price listener error", symbol=symbol, error=str(exc))

    def _notify_dom_listeners(self, symbol: str, levels: Sequence[Mapping[str, Any]]) -> None:
        listeners: List[Callable[[str, Sequence[Mapping[str, Any]]], None]]
        with self._market_data_lock:
            listeners = list(self._dom_listeners)
        for callback in listeners:
            try:
                callback(symbol, levels)
            except Exception as exc:  # pragma: no cover - defensive guard
                ibkr_logger.debug("DOM listener error", symbol=symbol, error=str(exc))

    def _notify_disconnect_listeners(self) -> None:
        listeners: List[Callable[[], None]]
        with self._market_data_lock:
            listeners = list(self._disconnect_listeners)
        for callback in listeners:
            try:
                callback()
            except Exception as exc:  # pragma: no cover - defensive guard
                ibkr_logger.debug("Disconnect listener error", error=str(exc))

    def _detach_subscription_handlers(self, subscription: _MarketDataSubscription) -> None:
        try:
            subscription.price_ticker.updateEvent -= subscription.price_handler
        except Exception:
            try:
                subscription.price_ticker.updateEvent.clear()
            except Exception:
                pass
        if subscription.dom_ticker is not None and subscription.dom_handler is not None:
            try:
                subscription.dom_ticker.updateEvent -= subscription.dom_handler
            except Exception:
                try:
                    subscription.dom_ticker.updateEvent.clear()
                except Exception:
                    pass

    def _clear_market_data_streams(self) -> None:
        with self._market_data_lock:
            streams = list(self._market_data_streams.values())
            self._market_data_streams.clear()
        for subscription in streams:
            self._detach_subscription_handlers(subscription)

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
            print("# trade = await client.place_limit_order('AAPL', 1, 150.0, 'BUY')")
            print("# trade = await client.place_limit_order('MNQ', 1, 23360, 'BUY')")
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
        asyncio.run(client.disconnect())
        print("Debug session completed")
