"""Compatibility shims exposing Interactive Brokers datatypes.

This module centralises the import logic for contract and order classes so the
rest of the code base can rely on a single source regardless of whether
``ib_async`` is installed.  When the real dependency is missing (as in CI) we
fall back to the asynchronous stub that ships with the test suite.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Default assignments avoid ``NameError`` in environments where neither the
# real library nor the stub can be imported for some reason.  The actual import
# attempts below will overwrite these placeholders.
Contract: Any = None
Future: Any = None
Stock: Any = None
Order: Any = None
LimitOrder: Any = None
MarketOrder: Any = None
DOMLevel: Any = None
MktDepthData: Any = None
IB: Any = None


def _import_from_ib_async() -> None:
    """Attempt to load objects from ``ib_async`` if it is installed."""

    global Contract, Future, Stock, Order, LimitOrder, MarketOrder, DOMLevel, MktDepthData, IB

    try:  # pragma: no cover - exercised only when dependency is available
        from ib_async import (  # type: ignore
            Contract as _Contract,
            Future as _Future,
            IB as _IB,
            LimitOrder as _LimitOrder,
            MarketOrder as _MarketOrder,
            Order as _Order,
            Stock as _Stock,
        )
        try:  # pragma: no cover - optional extras
            from ib_async.objects import (  # type: ignore
                DOMLevel as _DOMLevel,
                MktDepthData as _MktDepthData,
            )
        except Exception:
            _DOMLevel = None
            _MktDepthData = None
    except Exception:  # pragma: no cover - dependency layout may differ
        try:
            from ib_async.contracts import (  # type: ignore
                Contract as _Contract,
                Future as _Future,
                Stock as _Stock,
            )
            from ib_async.orders import (  # type: ignore
                LimitOrder as _LimitOrder,
                MarketOrder as _MarketOrder,
                Order as _Order,
            )
            from ib_async.client import IB as _IB  # type: ignore
            try:  # pragma: no cover - optional extras
                from ib_async.objects import (  # type: ignore
                    DOMLevel as _DOMLevel,
                    MktDepthData as _MktDepthData,
                )
            except Exception:
                _DOMLevel = None
                _MktDepthData = None
        except Exception:
            return

    Contract = _Contract
    Future = _Future
    Stock = _Stock
    Order = _Order
    LimitOrder = _LimitOrder
    MarketOrder = _MarketOrder
    DOMLevel = _DOMLevel
    MktDepthData = _MktDepthData
    IB = _IB


def _import_from_stub() -> None:
    """Fallback import used in test environments without ``ib_async``."""

    global Contract, Future, Stock, Order, LimitOrder, MarketOrder, DOMLevel, MktDepthData, IB

    from test.ib_insync_stub import (  # type: ignore
        Contract as _Contract,
        Future as _Future,
        IB as _IB,
        LimitOrder as _LimitOrder,
        MarketOrder as _MarketOrder,
        Order as _Order,
        Stock as _Stock,
        DOMLevel as _DOMLevel,
        MktDepthData as _MktDepthData,
    )

    Contract = _Contract
    Future = _Future
    Stock = _Stock
    Order = _Order
    LimitOrder = _LimitOrder
    MarketOrder = _MarketOrder
    DOMLevel = _DOMLevel
    MktDepthData = _MktDepthData
    IB = _IB


_import_from_ib_async()

if Contract is None:
    _import_from_stub()


__all__ = [
    "Contract",
    "Future",
    "Stock",
    "Order",
    "LimitOrder",
    "MarketOrder",
    "DOMLevel",
    "MktDepthData",
    "IB",
]


if DOMLevel is None:
    @dataclass
    class _FallbackDOMLevel:
        price: float
        size: float
        marketMaker: str

    DOMLevel = _FallbackDOMLevel


if MktDepthData is None:
    @dataclass
    class _FallbackMktDepthData:
        time: Any
        position: int
        marketMaker: str
        operation: int
        side: int
        price: float
        size: float

    MktDepthData = _FallbackMktDepthData
