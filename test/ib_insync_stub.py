"""Lightweight fallbacks for the optional :mod:`ib_insync` dependency.

This project primarily targets Interactive Brokers via the third-party
``ib_insync`` package.  The full client is sizeable and not always available
in constrained environments such as CI or development containers.  When the
real dependency is missing we still want the FastAPI application to boot so
that HTTP endpoints (which mostly gate on the connection status) can be
tested.  Importing the real package would normally raise :class:`ImportError`
and abort start-up.

To keep the application responsive we provide a collection of minimal stub
implementations that mimic the handful of attributes accessed by the web
API.  The stubs intentionally avoid any real network activity – every method
either returns a harmless default or raises a descriptive runtime error.  The
goal is merely to let routes query the connection state (which will always be
``False``) and respond with the expected "Not connected" messages used in the
test-suite.

These classes are *not* complete replacements for ``ib_insync``; they only
cover the narrow surface used by our offline tests.  The real dependency will
be used automatically whenever it is installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Iterable, List


class _DummyEvent:
    """Very small stand-in for ``ib_insync.eventkit.Event``.

    The real ``Event`` type behaves like a set of callables and supports the
    ``+=``/``-=`` operators.  We only need enough behaviour so that attaching
    and removing handlers in the connection manager does not crash.
    """

    def __init__(self) -> None:
        self._handlers: List[Any] = []

    def __iadd__(self, handler):
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

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        for handler in list(self._handlers):
            try:
                handler(*args, **kwargs)
            except Exception:
                # Handlers in the real implementation swallow exceptions; we
                # follow the same philosophy for the stub to avoid secondary
                # crashes during testing.
                pass


class _DummyClient:
    """Mimic the ``ib_insync.client.Client`` constants used by the app."""

    DISCONNECTED = 0
    CONNECTING = 1
    CONNECTED = 2

    def __init__(self) -> None:
        self.connState = self.DISCONNECTED


class IB:
    """Simplified ``IB`` session that always reports as disconnected."""

    def __init__(self) -> None:
        self.client = _DummyClient()
        self.wrapper = SimpleNamespace(
            error=lambda *args, **kwargs: None,
            completedOrder=lambda *args, **kwargs: None,
        )
        self.orderStatusEvent = _DummyEvent()
        self.execDetailsEvent = _DummyEvent()
        self.commissionReportEvent = _DummyEvent()

    async def connectAsync(self, *args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("ib_insync is not installed; cannot establish IBKR connection")

    def disconnect(self) -> None:
        self.client.connState = self.client.DISCONNECTED

    def isConnected(self) -> bool:
        return False

    def reqMarketDataType(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def qualifyContractsAsync(self, contract: Any) -> List[Any]:
        return [contract]

    async def reqHistoricalDataAsync(self, *args: Any, **kwargs: Any) -> list:
        return []

    def placeOrder(self, contract: Any, order: Any) -> Any:
        # Return a very small object with the attribute access pattern that the
        # production code expects (``trade.order.orderId``).
        return SimpleNamespace(order=SimpleNamespace(orderId=-1), contract=contract)


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
        # Ensure the common attributes exist so attribute access never fails.
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
class Order:
    """Data container roughly matching ``ib_insync.order.Order``."""

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

