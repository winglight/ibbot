"""IB market data feed service components."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = ["MarketDataFeedService"]


if TYPE_CHECKING:  # pragma: no cover - only used for type checkers
    from .main import MarketDataFeedService


def __getattr__(name: str) -> Any:
    """Provide lazy access to the ``MarketDataFeedService`` class.

    Importing :mod:`services.ib_feed.main` at module import time triggers a
    ``RuntimeWarning`` when the service is executed via ``python -m``. To avoid
    eagerly importing ``main`` while still re-exporting ``MarketDataFeedService``
    for convenience, the attribute is loaded on demand when accessed.
    """

    if name == "MarketDataFeedService":
        from .main import MarketDataFeedService as service

        return service
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
