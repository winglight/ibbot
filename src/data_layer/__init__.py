"""Unified market data layer scaffolding.

This package hosts the core infrastructure for the unified market data
platform.  Initial components include an in-process event bus, a data
source manager facade, and the configuration helpers required to toggle
between legacy and unified modes.  Concrete data adapters will be added
in subsequent iterations.
"""

from .config import DataLayerMode, get_data_layer_mode, is_dual_mode, is_legacy_mode, is_unified_mode
from .cache import CacheBackend, InMemoryCacheBackend
from .data_source_manager import (
    DataSourceAdapter,
    DataSourceError,
    DataSourceManager,
    DataSubscriptionRequest,
    DataSubscriptionToken,
)
from .event_bus import EventBus, EventEnvelope, BusSubscriptionToken
from .persistence import PersistenceBackend, NoopPersistenceBackend

__all__ = [
    "DataLayerMode",
    "get_data_layer_mode",
    "is_dual_mode",
    "is_legacy_mode",
    "is_unified_mode",
    "CacheBackend",
    "InMemoryCacheBackend",
    "DataSourceAdapter",
    "DataSourceError",
    "DataSourceManager",
    "DataSubscriptionRequest",
    "DataSubscriptionToken",
    "EventBus",
    "EventEnvelope",
    "BusSubscriptionToken",
    "PersistenceBackend",
    "NoopPersistenceBackend",
]
