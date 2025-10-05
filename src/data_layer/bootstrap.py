"""Bootstrap helpers for wiring the unified data layer."""

from __future__ import annotations

import os
from typing import Optional

from loguru import logger

from src.dom_dependencies import depth_subscriber

from .adapters import DomDepthAdapter, MarketQuoteAdapter
from .cache import InMemoryCacheBackend, RedisCacheBackend
from .data_source_manager import DataSourceManager
from .config import get_candle_intervals
from .persistence import (
    MariaDbPersistenceBackend,
    NoopPersistenceBackend,
    ParquetPersistenceBackend,
)
from src.ibbot.storage.parquet_writer import MarketDataParquetWriter
from .quote_feed import QuoteFeed, resolve_default_quote_feed
from src.ibbot.redis import DEFAULT_REDIS_URL
from src.db import (
    MariaDBConfigurationError,
    connect as db_connect,
    market_event_writer,
)
from src.services.factories import create_service_metrics_writer

__all__ = ["get_data_source_manager", "get_quote_feed"]


_manager: Optional[DataSourceManager] = None
_quote_feed: Optional[QuoteFeed] = None
_cache_backend = None
_persistence_backend = None


def get_quote_feed() -> QuoteFeed:
    global _quote_feed
    if _quote_feed is None:
        _quote_feed = resolve_default_quote_feed()
        logger.info("Initialised default quote feed: {}", _quote_feed.__class__.__name__)
    return _quote_feed


def get_data_source_manager() -> DataSourceManager:
    """Return the global data source manager instance."""

    global _manager
    if _manager is not None:
        return _manager

    manager = DataSourceManager()
    dom_adapter = DomDepthAdapter(depth_subscriber, manager.event_bus)
    manager.register_source(dom_adapter)

    intervals, history_limit = get_candle_intervals()
    quote_adapter = MarketQuoteAdapter(
        get_quote_feed(),
        manager.event_bus,
        _get_cache_backend(),
        _get_persistence_backend(),
        intervals=intervals,
        candle_history=history_limit,
    )
    manager.register_source(quote_adapter)
    _manager = manager
    logger.info("Data source manager initialised with DOM and market quote adapters")
    return manager


def _get_cache_backend():
    global _cache_backend
    if _cache_backend is not None:
        return _cache_backend

    redis_url = None
    for key in ("DATA_LAYER_REDIS_URL", "IBBOT_REDIS_URL"):
        candidate = os.getenv(key, "").strip()
        if candidate:
            redis_url = candidate
            break

    if not redis_url:
        redis_url = DEFAULT_REDIS_URL

    prefix = os.getenv("DATA_LAYER_REDIS_PREFIX", "market-data").strip() or "market-data"
    prefix = prefix.rstrip(":")
    ttl_raw = os.getenv("DATA_LAYER_REDIS_SNAPSHOT_TTL")
    ttl = None
    if ttl_raw:
        try:
            ttl_value = int(ttl_raw.strip())
        except ValueError:
            ttl_value = None
        else:
            if ttl_value > 0:
                ttl = ttl_value

    try:
        _cache_backend = RedisCacheBackend(redis_url, prefix=prefix, snapshot_ttl=ttl)
        logger.info("Using Redis cache backend at {}", redis_url)
        return _cache_backend
    except Exception as exc:
        logger.warning("Failed to initialise Redis cache backend: {}", exc)

    _cache_backend = InMemoryCacheBackend()
    logger.info("Using in-memory cache backend")
    return _cache_backend


def _get_persistence_backend():
    global _persistence_backend
    if _persistence_backend is not None:
        return _persistence_backend

    storage_path = (
        os.getenv("DATA_LAYER_PARQUET_PATH")
        or os.getenv("MARKET_DATA_STORAGE_PATH")
        or "data/market_data"
    )
    compression = os.getenv("MARKET_DATA_PARQUET_COMPRESSION", "snappy")

    try:
        writer = MarketDataParquetWriter(base_dir=storage_path, compression=compression)
        _persistence_backend = ParquetPersistenceBackend(writer)
        logger.info("Using Parquet persistence backend at {}", storage_path)
        return _persistence_backend
    except Exception as exc:
        logger.warning("Failed to initialise Parquet backend: {}", exc)

    try:
        with db_connect():
            pass
    except MariaDBConfigurationError as exc:
        logger.info("MariaDB persistence disabled: {}", exc)
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Failed to initialise MariaDB persistence backend: {}", exc)
    else:
        _persistence_backend = MariaDbPersistenceBackend(
            repository_factory=market_event_writer,
            metric_writer_factory=lambda: create_service_metrics_writer(
                logger_name="data-layer.metrics"
            ),
        )
        logger.info("Using MariaDB persistence backend")
        return _persistence_backend

    _persistence_backend = NoopPersistenceBackend()
    logger.info("Using noop persistence backend")
    return _persistence_backend
