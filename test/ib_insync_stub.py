"""Async-compatible fallbacks for environments without :mod:`ib_async`.

The production codebase now targets the coroutine focused ``ib_async``
library.  Local development containers and CI jobs may not ship with the
dependency installed which used to cause imports to fail.  These lightweight
stubs provide a minimal surface area that mirrors the subset of behaviours the
application relies upon so modules can continue to load and tests can run
without a live Interactive Brokers gateway.

The stubs intentionally avoid any network interaction – methods either return
harmless defaults or raise :class:`RuntimeError` to indicate that the real
library is required for full functionality.  They implement both the legacy
``ib_insync`` method names as well as the new ``ib_async`` style helpers so the
compatibility adapter can interact with them transparently during the
migration.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Sequence


class _StubEvent:
    """Very small stand-in for ``ib_async``/``ib_insync`` event containers."""

    def __init__(self) -> None:
        self._handlers: list[Any] = []

    def __iadd__(self, handler):  # pragma: no cover - trivial container
        if handler not in self._handlers:
            self._handlers.append(handler)
        return self

    def __isub__(self, handler):  # pragma: no cover - defensive, rarely used
        try:
            self._handlers.remove(handler)
        except ValueError:
            pass
        return self

    def clear(self) -> None:
        self._handlers.clear()

    def __call__(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover -
        for handler in tuple(self._handlers):
            try:
                handler(*args, **kwargs)
            except Exception:
                # Keep handler failures isolated like the real implementation.
                pass


class _StubTicker:
    """Ticker object exposing market data and depth events."""

    def __init__(self) -> None:
        self.updateEvent = _StubEvent()
        self.update_event = self.updateEvent
        self.tickEvent = self.updateEvent
        self.domUpdateEvent = _StubEvent()
        self.dom_update_event = self.domUpdateEvent
        self.updateMktDepthL2 = self.domUpdateEvent


class _StubClient:
    """Mimic the ``Client`` connection state attributes."""

    DISCONNECTED = 0
    CONNECTING = 1
    CONNECTED = 2

    def __init__(self) -> None:
        self.connState = self.DISCONNECTED


class IB:
    """Simplified async-friendly session that never connects to IBKR."""

    def __init__(self) -> None:
        self.client = _StubClient()
        self.wrapper = SimpleNamespace(
            error=lambda *args, **kwargs: None,
            completedOrder=lambda *args, **kwargs: None,
        )
        # Shared events used across the application.
        self.error_event = _StubEvent()
        self.errorEvent = self.error_event
        self.depth_event = _StubEvent()
        self.updateMktDepthL2 = self.depth_event
        self.orderStatusEvent = _StubEvent()
        self.execDetailsEvent = _StubEvent()
        self.commissionReportEvent = _StubEvent()

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------
    async def connect(self, *args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("ib_async is not installed; cannot establish IBKR connection")

    async def connectAsync(self, *args: Any, **kwargs: Any) -> bool:
        return await self.connect(*args, **kwargs)

    def disconnect(self) -> None:
        self.client.connState = self.client.DISCONNECTED

    def close(self) -> None:  # pragma: no cover - alias used by adapter
        self.disconnect()

    def isConnected(self) -> bool:
        return False

    def is_connected(self) -> bool:
        return False

    def reqMarketDataType(self, *args: Any, **kwargs: Any) -> None:
        return None

    # ------------------------------------------------------------------
    # Market data & depth helpers
    # ------------------------------------------------------------------
    def reqMktData(self, *args: Any, **kwargs: Any) -> _StubTicker:
        return _StubTicker()

    def reqMarketData(self, *args: Any, **kwargs: Any) -> _StubTicker:
        return self.reqMktData(*args, **kwargs)

    def request_market_data(self, *args: Any, **kwargs: Any) -> _StubTicker:
        return self.reqMktData(*args, **kwargs)

    def cancelMktData(self, *args: Any, **kwargs: Any) -> None:
        return None

    def cancel_market_data(self, *args: Any, **kwargs: Any) -> None:
        return None

    def reqMktDepth(self, *args: Any, **kwargs: Any) -> _StubTicker:
        return _StubTicker()

    def request_market_depth(self, *args: Any, **kwargs: Any) -> _StubTicker:
        return self.reqMktDepth(*args, **kwargs)

    def reqMktDepthEx(self, *args: Any, **kwargs: Any) -> _StubTicker:
        return self.reqMktDepth(*args, **kwargs)

    def cancelMktDepth(self, *args: Any, **kwargs: Any) -> None:
        return None

    def cancel_market_depth(self, *args: Any, **kwargs: Any) -> None:
        return None

    # ------------------------------------------------------------------
    # Historical data and contract helpers
    # ------------------------------------------------------------------
    async def qualifyContractsAsync(self, *contracts: Any) -> Sequence[Any]:
        return contracts

    async def qualifyContracts(self, *contracts: Any) -> Sequence[Any]:
        return await self.qualifyContractsAsync(*contracts)

    async def qualify_contracts(self, *contracts: Any) -> Sequence[Any]:
        return await self.qualifyContractsAsync(*contracts)

    async def reqHistoricalDataAsync(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    async def reqHistoricalData(self, *args: Any, **kwargs: Any) -> list[Any]:
        return await self.reqHistoricalDataAsync(*args, **kwargs)

    async def request_historical_data(self, *args: Any, **kwargs: Any) -> list[Any]:
        return await self.reqHistoricalDataAsync(*args, **kwargs)

    # ------------------------------------------------------------------
    # Order and account helpers
    # ------------------------------------------------------------------
    def placeOrder(self, contract: Any, order: Any) -> Any:
        return SimpleNamespace(order=SimpleNamespace(orderId=-1), contract=contract)

    async def place_order(self, contract: Any, order: Any) -> Any:
        return self.placeOrder(contract, order)

    def cancelOrder(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def cancel_order(self, *args: Any, **kwargs: Any) -> None:
        return None

    def reqOpenOrders(self) -> list[Any]:
        return []

    def openOrders(self) -> list[Any]:  # pragma: no cover - alias
        return []

    async def list_open_orders(self) -> list[Any]:
        return []

    def reqAccountSummary(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    def accountSummary(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    async def account_summary(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    def reqPositions(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    def positions(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    async def portfolio(self) -> list[Any]:
        return []


class ExecutionFilter:
    """Placeholder mirroring the constructor signature of the real type."""

    def __init__(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


class Contract:
    """Base contract storing attributes passed to the constructor."""

    def __init__(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)
        self.secType = getattr(self, "secType", None)
        self.symbol = getattr(self, "symbol", None)
        self.exchange = getattr(self, "exchange", None)
        self.currency = getattr(self, "currency", None)


class Stock(Contract):
    def __init__(self, symbol: str, exchange: str = "SMART", currency: str = "USD") -> None:
        super().__init__(symbol=symbol, exchange=exchange, currency=currency, secType="STK")


class Future(Contract):
    def __init__(self, symbol: str, exchange: str = "SMART", currency: str = "USD") -> None:
        super().__init__(symbol=symbol, exchange=exchange, currency=currency, secType="FUT")


@dataclass
class DOMLevel:
    """Lightweight representation of a DOM price level."""

    price: float
    size: float
    marketMaker: str


@dataclass
class MktDepthData:
    """Simplified tick used by the depth guard to queue updates."""

    time: Any
    position: int
    marketMaker: str
    operation: int
    side: int
    price: float
    size: float


@dataclass
class Order:
    """Data container roughly matching the real order object."""

    action: str
    totalQuantity: int
    orderType: str
    lmtPrice: float | None = None


class MarketOrder(Order):
    def __init__(self, action: str, quantity: int) -> None:
        super().__init__(action=action, totalQuantity=quantity, orderType="MKT")


class LimitOrder(Order):
    def __init__(self, action: str, quantity: int, limit_price: float) -> None:
        super().__init__(
            action=action,
            totalQuantity=quantity,
            orderType="LMT",
            lmtPrice=limit_price,
        )


# ``util`` is only used for its ``startLoop`` helper in our code-base.  Using a
# simple namespace keeps the call-sites intact without pulling additional
# dependencies.
util = SimpleNamespace(startLoop=lambda *args, **kwargs: None)


__all__ = [
    "IB",
    "Contract",
    "Future",
    "Stock",
    "Order",
    "MarketOrder",
    "LimitOrder",
    "ExecutionFilter",
    "util",
]

