"""Bootstrap helpers for wiring the unified data layer."""

from __future__ import annotations

from typing import Optional

from loguru import logger

from src.dom_dependencies import depth_subscriber

from .adapters import DomDepthAdapter, MarketQuoteAdapter
from .cache import InMemoryCacheBackend
from .data_source_manager import DataSourceManager
from .persistence import NoopPersistenceBackend
from .quote_feed import QuoteFeed, resolve_default_quote_feed

__all__ = ["get_data_source_manager", "get_quote_feed"]


_manager: Optional[DataSourceManager] = None
_quote_feed: Optional[QuoteFeed] = None
_cache_backend = InMemoryCacheBackend()
_persistence_backend = NoopPersistenceBackend()


def get_quote_feed() -> QuoteFeed:
    global _quote_feed
    if _quote_feed is None:
        _quote_feed = resolve_default_quote_feed()
        logger.info("Initialised default quote feed: %s", _quote_feed.__class__.__name__)
    return _quote_feed


def get_data_source_manager() -> DataSourceManager:
    """Return the global data source manager instance."""

    global _manager
    if _manager is not None:
        return _manager

    manager = DataSourceManager()
    dom_adapter = DomDepthAdapter(depth_subscriber, manager.event_bus)
    manager.register_source(dom_adapter)

    quote_adapter = MarketQuoteAdapter(
        get_quote_feed(),
        manager.event_bus,
        _cache_backend,
        _persistence_backend,
    )
    manager.register_source(quote_adapter)
    _manager = manager
    logger.info("Data source manager initialised with DOM and market quote adapters")
    return manager
