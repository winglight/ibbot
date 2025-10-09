"""Lightweight compatibility shims that replace :mod:`ib_insync` usage.

The legacy codebase historically reached for the ``ib_insync`` convenience
wrapper on top of the official TWS API.  As part of the refactor we want the
runtime to depend exclusively on ``ibapi`` while still keeping the handful of
helpers (``Contract`` containers, ``Order`` data classes, simple event hooks)
that higher level modules interact with.  Re-implementing the tiny subset of
behaviour we rely on keeps those call sites stable without importing the third
party dependency.

None of the classes below attempt to be drop in replacements for the real
library; they merely satisfy the attribute access patterns exercised by the
application and unit tests.  Anything that would have triggered real network
traffic either raises a descriptive ``RuntimeError`` or returns harmless
defaults so the surrounding logic can continue operating in offline mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable, List, Optional


class _Event:
    """Very small stand-in for :class:`ib_insync.eventkit.Event`."""

    def __init__(self) -> None:
        self._handlers: List[Callable[..., Any]] = []

    def __iadd__(self, handler: Callable[..., Any]):
        self._handlers.append(handler)
        return self

    def __isub__(self, handler: Callable[..., Any]):  # pragma: no cover - defensive
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
                # ``ib_insync`` swallows handler exceptions; keeping the same
                # behaviour avoids secondary crashes during cleanup paths.
                pass


class _Client:
    """Subset of :class:`ib_insync.client.Client` used for connection tracking."""

    DISCONNECTED = 0
    CONNECTING = 1
    CONNECTED = 2

    def __init__(self) -> None:
        self.connState = self.DISCONNECTED
        self.host: Optional[str] = None
        self.port: Optional[int] = None


class IB:
    """Compatibility wrapper that mimics ``ib_insync.IB`` interactions."""

    def __init__(self) -> None:
        self.client = _Client()
        self.wrapper = SimpleNamespace(
            error=lambda *args, **kwargs: None,
            completedOrder=lambda *args, **kwargs: None,
        )
        self.orderStatusEvent = _Event()
        self.execDetailsEvent = _Event()
        self.commissionReportEvent = _Event()

    async def connectAsync(self, *args: Any, **kwargs: Any) -> bool:
        raise RuntimeError(
            "ib_insync compatibility mode does not provide network connections"
        )

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
        return SimpleNamespace(order=SimpleNamespace(orderId=-1), contract=contract)


class ExecutionFilter:
    """Placeholder mirroring :class:`ib_insync.client.ExecutionFilter`."""

    def __init__(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


class Contract:
    """Lightweight contract container."""

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
class Order:
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


@dataclass
class DOMLevel:
    """Minimal representation of ``ib_insync.objects.DOMLevel``."""

    price: float
    size: float
    marketMaker: str | None = None


@dataclass
class MktDepthData:
    """Compatibility container for depth snapshots."""

    position: int
    marketMaker: str
    operation: int
    side: int
    price: float
    size: float


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
    "DOMLevel",
    "MktDepthData",
    "util",
]

