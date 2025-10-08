"""Test double for the official ``ibapi`` client/wrapper stack.

The production code dynamically falls back to this stub whenever the real
``ibapi`` package is unavailable (which is the default in CI).  The
implementation mirrors only the behaviour that our unit tests rely on:

* ``EWrapper`` base class providing overridable callbacks.
* ``EClient`` implementing ``connect``, ``disconnect``, ``run`` and
  ``isConnected``.
* Minimal request/response handling for ``reqContractDetails`` so that unit
  tests can exercise the asyncio bridges without a real gateway.
* Basic connection state tracking that mimics the ``ib_insync`` stub that used
  to power the legacy code base.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Contract:
    """Very small stand-in for :class:`ibapi.contract.Contract`."""

    symbol: str = ""
    secType: str = ""
    currency: str = ""
    exchange: str = ""
    lastTradeDateOrContractMonth: str = ""
    tradingClass: str = ""
    multiplier: str = ""


@dataclass
class ContractDetails:
    """Lightweight mimic of :class:`ibapi.contract.ContractDetails`."""

    contract: Contract = field(default_factory=Contract)


@dataclass
class TickAttrib:
    """Simplified stand-in for :class:`ibapi.ticktype.TickAttrib`."""

    canAutoExecute: bool = False
    pastLimit: bool = False
    preOpen: bool = False


@dataclass
class BarData:
    """Simplified representation of :class:`ibapi.common.BarData`."""

    date: str = ""
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0


@dataclass
class HistoricalTick:
    """Minimal stand-in for :class:`ibapi.common.HistoricalTick`."""

    time: int = 0
    price: float = 0.0
    size: float = 0.0


@dataclass
class HistoricalTickBidAsk:
    """Simplified version of :class:`ibapi.common.HistoricalTickBidAsk`."""

    time: int = 0
    priceBid: float = 0.0
    priceAsk: float = 0.0
    sizeBid: float = 0.0
    sizeAsk: float = 0.0


@dataclass
class HistoricalTickLast:
    """Simplified version of :class:`ibapi.common.HistoricalTickLast`."""

    time: int = 0
    price: float = 0.0
    size: float = 0.0
    exchange: str = ""
    specialConditions: str = ""


@dataclass
class Order:
    """Minimal representation of :class:`ibapi.order.Order`."""

    orderId: int = 0
    action: str = ""
    totalQuantity: float = 0.0
    orderType: str = ""
    lmtPrice: float = 0.0
    auxPrice: float = 0.0
    tif: str = "GTC"
    clientId: int = 0
    parentId: int = 0
    orderRef: str = ""
    permId: int = 0


@dataclass
class OrderState:
    """Lightweight mimic of :class:`ibapi.order_state.OrderState`."""

    status: str = "Submitted"
    filled: float = 0.0
    remaining: float = 0.0
    avgFillPrice: float = 0.0
    lastFillPrice: float = 0.0
    submittedTime: str = ""
    completedTime: str = ""
    warningText: str = ""
    whyHeld: str = ""
    commission: float = 0.0
    commissionCurrency: str = ""
    realizedPNL: float = 0.0


@dataclass
class Execution:
    """Simplified stand-in for :class:`ibapi.execution.Execution`."""

    execId: str = ""
    orderId: int = 0
    clientId: int = 0
    permId: int = 0
    time: str = ""
    acctNumber: str = ""
    exchange: str = ""
    side: str = ""
    shares: float = 0.0
    price: float = 0.0
    cumQty: float = 0.0
    avgPrice: float = 0.0


@dataclass
class CommissionReport:
    """Minimal version of :class:`ibapi.commission_report.CommissionReport`."""

    execId: str = ""
    commission: float = 0.0
    currency: str = "USD"
    realizedPNL: float = 0.0
    yield_: float = 0.0
    yieldRedemptionDate: int = 0


@dataclass
class AccountValue:
    """Lightweight container mimicking account value snapshots."""

    tag: str
    value: str
    currency: str
    account: str


@dataclass
class PortfolioItem:
    """Simplified representation of ``updatePortfolio`` payloads."""

    contract: Contract
    position: float
    marketPrice: float
    marketValue: float
    averageCost: float
    unrealizedPNL: float
    realizedPNL: float
    account: str = ""


class EWrapper:
    """Mirror the interface of ``ibapi.wrapper.EWrapper``."""

    def nextValidId(self, orderId: int) -> None:  # noqa: N802 - IB API naming
        pass

    def error(self, reqId: int, errorCode: int, errorString: str, advancedOrderRejectJson: str = "") -> None:  # noqa: N802,E501
        pass

    def connectAck(self) -> None:  # noqa: N802 - IB API naming
        pass

    def connectionClosed(self) -> None:  # noqa: N802 - IB API naming
        pass


class EClient:
    """Very small stand-in for :class:`ibapi.client.EClient`."""

    DISCONNECTED = 0
    CONNECTING = 1
    CONNECTED = 2

    def __init__(self, wrapper: EWrapper) -> None:
        self.wrapper = wrapper
        self.connState = self.DISCONNECTED
        self._host = ""
        self._port = 0
        self.clientId: Optional[int] = None
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._contract_details_requests: Dict[int, Contract] = {}
        self._market_data_requests: Dict[int, Contract] = {}
        self._market_depth_requests: Dict[int, Contract] = {}
        self._historical_data_requests: Dict[int, Dict[str, object]] = {}
        self._historical_ticks_requests: Dict[int, Dict[str, object]] = {}
        self._orders: Dict[int, Order] = {}
        self._order_contracts: Dict[int, Contract] = {}
        self._auto_ack_orders = True
        self._account_summary_requests: Dict[int, Dict[str, str]] = {}
        self._account_updates_subscribed = False
        self._account_updates_account: str = ""
        self._positions_requested = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def connect(self, host: str, port: int, client_id: int) -> int:
        self._host = host
        self._port = port
        self.clientId = client_id
        self.connState = self.CONNECTING
        self._connected.set()
        self.connState = self.CONNECTED
        self.wrapper.connectAck()
        # ``nextValidId`` is the first callback the real gateway sends after
        # a successful handshake; we mimic that to unblock async waiters.
        self.wrapper.nextValidId(1)
        return 0

    def disconnect(self) -> None:
        if not self.isConnected():
            return
        self.connState = self.DISCONNECTED
        self._stop.set()
        self._connected.clear()
        self.wrapper.connectionClosed()

    def isConnected(self) -> bool:
        return self.connState == self.CONNECTED

    def run(self) -> None:
        """Block until :meth:`disconnect` is called."""

        while not self._stop.is_set():
            time.sleep(0.01)
        self._stop.clear()

    # ------------------------------------------------------------------
    # Market/contract requests (minimal surface for tests)
    # ------------------------------------------------------------------
    def reqContractDetails(self, reqId: int, contract: Contract) -> None:  # noqa: N802 - API signature
        if not self.isConnected():
            raise RuntimeError("Not connected")
        self._contract_details_requests[reqId] = contract

    def cancelContractDetails(self, reqId: int) -> None:  # noqa: N802 - API signature
        self._contract_details_requests.pop(reqId, None)

    def reqMktData(  # noqa: N802 - API signature
        self,
        reqId: int,
        contract: Contract,
        genericTickList: str,
        snapshot: bool,
        regulatorySnapshot: bool,
        mktDataOptions: Optional[list] = None,
    ) -> None:
        if not self.isConnected():
            raise RuntimeError("Not connected")
        self._market_data_requests[reqId] = contract

    def cancelMktData(self, reqId: int) -> None:  # noqa: N802 - API signature
        self._market_data_requests.pop(reqId, None)

    def reqMktDepth(  # noqa: N802 - API signature
        self,
        reqId: int,
        contract: Contract,
        numRows: int,
        isSmartDepth: bool,
        mktDepthOptions: Optional[list] = None,
    ) -> None:
        if not self.isConnected():
            raise RuntimeError("Not connected")
        self._market_depth_requests[reqId] = contract

    def cancelMktDepth(self, reqId: int, isSmartDepth: bool = False) -> None:  # noqa: N802 - API signature
        self._market_depth_requests.pop(reqId, None)

    def reqHistoricalData(  # noqa: N802 - API signature
        self,
        reqId: int,
        contract: Contract,
        endDateTime: str,
        durationStr: str,
        barSizeSetting: str,
        whatToShow: str,
        useRth: bool,
        formatDate: int,
        keepUpToDate: bool,
        chartOptions: Optional[List] = None,
    ) -> None:
        if not self.isConnected():
            raise RuntimeError("Not connected")
        self._historical_data_requests[reqId] = {
            "contract": contract,
            "end": endDateTime,
            "duration": durationStr,
            "bar_size": barSizeSetting,
            "what": whatToShow,
            "use_rth": useRth,
            "format": formatDate,
            "keep_up_to_date": keepUpToDate,
            "options": list(chartOptions or []),
        }

    def cancelHistoricalData(self, reqId: int) -> None:  # noqa: N802 - API signature
        self._historical_data_requests.pop(reqId, None)

    def reqHistoricalTicks(  # noqa: N802 - API signature
        self,
        reqId: int,
        contract: Contract,
        startDateTime: str,
        endDateTime: str,
        numberOfTicks: int,
        whatToShow: str,
        useRth: bool,
        ignoreSize: bool,
        miscOptions: Optional[List] = None,
    ) -> None:
        if not self.isConnected():
            raise RuntimeError("Not connected")
        self._historical_ticks_requests[reqId] = {
            "contract": contract,
            "start": startDateTime,
            "end": endDateTime,
            "count": numberOfTicks,
            "what": whatToShow,
            "use_rth": useRth,
            "ignore_size": ignoreSize,
            "options": list(miscOptions or []),
        }

    def cancelHistoricalTicks(self, reqId: int) -> None:  # noqa: N802 - API signature
        self._historical_ticks_requests.pop(reqId, None)

    def placeOrder(self, orderId: int, contract: Contract, order: Order) -> None:  # noqa: N802 - API signature
        if not self.isConnected():
            raise RuntimeError("Not connected")
        if orderId <= 0:
            raise ValueError("orderId must be positive")

        order.orderId = orderId
        self._orders[orderId] = order
        self._order_contracts[orderId] = contract

        if self._auto_ack_orders:
            state = OrderState(
                status="Submitted",
                filled=0.0,
                remaining=getattr(order, "totalQuantity", 0.0),
            )
            self.wrapper.openOrder(orderId, contract, order, state)
            self.wrapper.orderStatus(
                orderId,
                state.status,
                state.filled,
                state.remaining,
                state.avgFillPrice,
                getattr(order, "permId", 0),
                getattr(order, "parentId", 0),
                state.lastFillPrice,
                self.clientId or 0,
                state.whyHeld,
                0.0,
            )

    def cancelOrder(self, orderId: int) -> None:  # noqa: N802 - API signature
        if not self.isConnected():
            raise RuntimeError("Not connected")

        order = self._orders.get(orderId)
        contract = self._order_contracts.get(orderId, Contract())
        if order is None:
            order = Order(orderId=orderId)
        state = OrderState(
            status="Cancelled",
            filled=0.0,
            remaining=getattr(order, "totalQuantity", 0.0),
        )
        self.wrapper.orderStatus(
            orderId,
            state.status,
            state.filled,
            state.remaining,
            state.avgFillPrice,
            getattr(order, "permId", 0),
            getattr(order, "parentId", 0),
            state.lastFillPrice,
            self.clientId or 0,
            state.whyHeld,
            0.0,
        )
        self._orders.pop(orderId, None)
        self._order_contracts.pop(orderId, None)

    def reqAccountSummary(self, reqId: int, groupName: str, tags: str) -> None:  # noqa: N802 - API signature
        if not self.isConnected():
            raise RuntimeError("Not connected")
        self._account_summary_requests[reqId] = {"group": groupName, "tags": tags}

    def cancelAccountSummary(self, reqId: int) -> None:  # noqa: N802 - API signature
        self._account_summary_requests.pop(reqId, None)

    def reqAccountUpdates(self, subscribe: bool, account: str) -> None:  # noqa: N802 - API signature
        if not self.isConnected():
            raise RuntimeError("Not connected")
        self._account_updates_subscribed = bool(subscribe)
        if subscribe:
            self._account_updates_account = account

    def reqPositions(self) -> None:  # noqa: N802 - API signature
        if not self.isConnected():
            raise RuntimeError("Not connected")
        self._positions_requested = True

    def cancelPositions(self) -> None:  # noqa: N802 - API signature
        self._positions_requested = False

    # ------------------------------------------------------------------
    # Helpers for unit tests to trigger callbacks
    # ------------------------------------------------------------------
    def emit_tick_price(
        self,
        reqId: int,
        tickType: int,
        price: float,
        attrib: Optional[TickAttrib] = None,
    ) -> None:
        attrib = attrib or TickAttrib()
        self.wrapper.tickPrice(reqId, tickType, price, attrib)

    def emit_tick_size(self, reqId: int, tickType: int, size: float) -> None:
        self.wrapper.tickSize(reqId, tickType, size)

    def emit_market_data_type(self, reqId: int, market_data_type: int) -> None:
        self.wrapper.marketDataType(reqId, market_data_type)

    def emit_depth_update(
        self,
        reqId: int,
        position: int,
        marketMaker: str,
        operation: int,
        side: int,
        price: float,
        size: float,
        isSmartDepth: bool = False,
    ) -> None:
        self.wrapper.updateMktDepthL2(
            reqId,
            position,
            marketMaker,
            operation,
            side,
            price,
            size,
            isSmartDepth,
        )

    def emit_historical_bar(self, reqId: int, bar: BarData) -> None:
        self.wrapper.historicalData(reqId, bar)

    def emit_historical_data_end(self, reqId: int) -> None:
        self.wrapper.historicalDataEnd(reqId, "", "")

    def emit_historical_ticks(
        self,
        reqId: int,
        ticks: List[object],
        *,
        done: bool = True,
    ) -> None:
        if ticks and isinstance(ticks[0], HistoricalTickBidAsk):
            self.wrapper.historicalTicksBidAsk(reqId, ticks, done)
        elif ticks and isinstance(ticks[0], HistoricalTickLast):
            self.wrapper.historicalTicksLast(reqId, ticks, done)
        else:
            self.wrapper.historicalTicks(reqId, ticks, done)

    def emit_historical_ticks_end(self, reqId: int) -> None:
        self.wrapper.historicalTicks(reqId, [], True)

    def emit_open_order(
        self,
        orderId: int,
        contract: Contract,
        order: Order,
        state: OrderState | None = None,
    ) -> None:
        state = state or OrderState(
            status="Submitted",
            filled=0.0,
            remaining=getattr(order, "totalQuantity", 0.0),
        )
        self.wrapper.openOrder(orderId, contract, order, state)

    def emit_order_status(
        self,
        orderId: int,
        status: str,
        *,
        filled: float = 0.0,
        remaining: float = 0.0,
        avg_fill_price: float = 0.0,
        last_fill_price: float = 0.0,
        perm_id: int = 0,
        parent_id: int = 0,
        client_id: int | None = None,
        why_held: str = "",
        mkt_cap_price: float = 0.0,
    ) -> None:
        self.wrapper.orderStatus(
            orderId,
            status,
            filled,
            remaining,
            avg_fill_price,
            perm_id,
            parent_id,
            last_fill_price,
            client_id if client_id is not None else self.clientId or 0,
            why_held,
            mkt_cap_price,
        )

    def emit_execution_detail(
        self,
        reqId: int,
        contract: Contract,
        execution: Execution,
    ) -> None:
        self.wrapper.execDetails(reqId, contract, execution)

    def emit_commission_report(self, report: CommissionReport) -> None:
        self.wrapper.commissionReport(report)

    def emit_account_summary(
        self,
        reqId: int,
        account: str,
        tag: str,
        value: str,
        currency: str,
    ) -> None:
        self.wrapper.accountSummary(reqId, account, tag, value, currency)

    def emit_account_summary_end(self, reqId: int) -> None:
        self.wrapper.accountSummaryEnd(reqId)

    def emit_account_value(
        self,
        tag: str,
        value: str,
        currency: str,
        account: str,
    ) -> None:
        self.wrapper.updateAccountValue(tag, value, currency, account)

    def emit_portfolio_update(
        self,
        contract: Contract,
        position: float,
        market_price: float,
        market_value: float,
        average_cost: float,
        unrealized_pnl: float,
        realized_pnl: float,
        account: str = "",
    ) -> None:
        self.wrapper.updatePortfolio(
            contract,
            position,
            market_price,
            market_value,
            average_cost,
            unrealized_pnl,
            realized_pnl,
            account,
        )

    def emit_account_download_end(self, account: str) -> None:
        self.wrapper.accountDownloadEnd(account)

    def emit_position(
        self,
        account: str,
        contract: Contract,
        position: float,
        avg_cost: float,
    ) -> None:
        self.wrapper.position(account, contract, position, avg_cost)

    def emit_position_end(self) -> None:
        self.wrapper.positionEnd()
