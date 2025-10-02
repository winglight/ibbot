"""Bootstrap helpers for wiring the unified data layer."""

from __future__ import annotations

import os
from typing import Optional

from loguru import logger

from src.dom_dependencies import depth_subscriber

from .adapters import DomDepthAdapter, MarketQuoteAdapter
from .cache import InMemoryCacheBackend, RedisCacheBackend
from .data_source_manager import DataSourceManager
from .persistence import InfluxPersistenceBackend, NoopPersistenceBackend
from .quote_feed import QuoteFeed, resolve_default_quote_feed
from src.ibbot.redis import DEFAULT_REDIS_URL

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

    quote_adapter = MarketQuoteAdapter(
        get_quote_feed(),
        manager.event_bus,
        _get_cache_backend(),
        _get_persistence_backend(),
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

    influx_url = os.getenv("DATA_LAYER_INFLUX_URL")
    influx_bucket = os.getenv("DATA_LAYER_INFLUX_BUCKET")
    influx_org = os.getenv("DATA_LAYER_INFLUX_ORG")
    if influx_url and influx_bucket and influx_org:
        token = os.getenv("DATA_LAYER_INFLUX_TOKEN")
        measurement = os.getenv("DATA_LAYER_INFLUX_MEASUREMENT", "market_events")
        timeout_raw = os.getenv("DATA_LAYER_INFLUX_TIMEOUT_MS")
        timeout = None
        if timeout_raw:
            try:
                timeout_candidate = int(timeout_raw.strip())
            except ValueError:
                logger.warning(
                    "Invalid DATA_LAYER_INFLUX_TIMEOUT_MS value %r - falling back to default",
                    timeout_raw,
                )
                timeout = None
            else:
                if timeout_candidate > 0:
                    timeout = timeout_candidate

        try:
            _persistence_backend = InfluxPersistenceBackend(
                influx_url,
                bucket=influx_bucket,
                org=influx_org,
                token=token,
                measurement=measurement,
                timeout=timeout,
            )
            logger.info("Using InfluxDB persistence backend at {}", influx_url)
            return _persistence_backend
        except Exception as exc:
            logger.warning("Failed to initialise InfluxDB backend: {}", exc)

    _persistence_backend = NoopPersistenceBackend()
    logger.info("Using noop persistence backend")
    return _persistence_backend
