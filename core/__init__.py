"""Core application wiring for the UI service.

The UI runtime only needs a subset of the original backend services.  This
module now exposes the lightweight primitives required by the FastAPI app while
leaving heavy risk/strategy managers to dedicated services.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from loguru import logger

from src.cache import init_redis
from src.connection_manager import ConnectionConfig, connection_manager
from src.database import get_db_manager
from src.dom_dependencies import DOMConfig, get_dom_processor
import src.dom_dependencies as dom_dependencies
from src.dom_service import get_dom_service
from src.ibkr_client import IBKRClient
from src.websocket_manager import websocket_manager

ibkr_client: Optional[IBKRClient] = None
"""Cached IBKR client instance shared by the UI process."""

redis_client = None
"""Handle to the optional Redis client initialised on demand."""

dom_config: DOMConfig = dom_dependencies.dom_config
dom_processor = get_dom_processor()
depth_subscriber = dom_dependencies.depth_subscriber


async def initialise_redis() -> None:
    """Initialise the Redis client without blocking the import path."""

    global redis_client
    try:
        redis_client = await init_redis()
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning(f"Redis initialisation skipped: {exc}")
        redis_client = None
        return

    if redis_client is None:
        logger.info("Redis client unavailable after initialisation")
    else:
        logger.info("Redis client ready")


def initialise_redis_sync() -> None:
    """Synchronous helper used by bootstrap scripts to initialise Redis."""

    try:
        asyncio.run(initialise_redis())
    except RuntimeError as exc:  # pragma: no cover - defensive guard
        logger.warning(f"Redis initialisation aborted: {exc}")


def set_ibkr_client(client: Optional[IBKRClient]) -> None:
    """Update the shared IBKR client reference used by the UI."""

    global ibkr_client
    ibkr_client = client


def get_ibkr_client() -> Optional[IBKRClient]:
    """Return the cached IBKR client instance, if available."""

    return ibkr_client


__all__ = [
    "connection_manager",
    "ConnectionConfig",
    "IBKRClient",
    "ibkr_client",
    "get_ibkr_client",
    "set_ibkr_client",
    "websocket_manager",
    "redis_client",
    "initialise_redis",
    "initialise_redis_sync",
    "dom_config",
    "dom_processor",
    "depth_subscriber",
    "get_dom_service",
    "get_db_manager",
]
