"""High level helpers for the ibbot MariaDB integration.

The package exposes session factories and repository constructors so that
services can easily compose database interactions::

    from src.ibbot.db import get_async_session, order_repository

    async with get_async_session() as session:  # AsyncSession is an async context manager
        repo = order_repository(session)
        await repo.upsert_order(order_payload)

Environment variables
---------------------
``IBBOT_DB_URL``
    Preferred async SQLAlchemy URL (``mysql+asyncmy``).
``DATA_LAYER_DB_URL``
    Legacy fallback used by the historical data layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .config import DatabaseConfigError, get_database_url, resolve_database_url
from .session import get_async_engine, get_async_session, get_session_factory, session_scope
from .schemas import (
    AuditLog,
    Base,
    MarketEvent,
    MarketPrice,
    Order,
    RiskConfigSnapshot,
    RiskRuleSnapshot,
    ServiceMetric,
    metadata,
)
from .repositories.events import MarketEventRepository
from .repositories.market import MariaDbMarketPriceRepository
from .repositories.metrics import ServiceMetricRepository
from .repositories.order import MariaDbOrderRepository
from .repositories.risk import RiskSnapshotRepository
from .repositories.audit import AuditLogRepository

if TYPE_CHECKING:  # pragma: no cover - import for type hints only
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "AuditLog",
    "AuditLogRepository",
    "Base",
    "DatabaseConfigError",
    "MarketEvent",
    "MarketEventRepository",
    "MarketPrice",
    "MariaDbMarketPriceRepository",
    "MariaDbOrderRepository",
    "Order",
    "RiskConfigSnapshot",
    "RiskRuleSnapshot",
    "RiskSnapshotRepository",
    "ServiceMetric",
    "ServiceMetricRepository",
    "get_async_engine",
    "get_async_session",
    "get_database_url",
    "get_session_factory",
    "metadata",
    "resolve_database_url",
    "session_scope",
    "audit_log_repository",
    "market_event_repository",
    "market_price_repository",
    "order_repository",
    "risk_snapshot_repository",
    "service_metric_repository",
]


def order_repository(session: "AsyncSession") -> MariaDbOrderRepository:
    """Return an order repository bound to *session*."""

    return MariaDbOrderRepository(session)


def market_price_repository(
    session: "AsyncSession",
) -> MariaDbMarketPriceRepository:
    """Return a market price repository bound to *session*."""

    return MariaDbMarketPriceRepository(session)


def service_metric_repository(
    session: "AsyncSession",
) -> ServiceMetricRepository:
    """Return a service metric repository bound to *session*."""

    return ServiceMetricRepository(session)


def risk_snapshot_repository(
    session: "AsyncSession",
) -> RiskSnapshotRepository:
    """Return a risk snapshot repository bound to *session*."""

    return RiskSnapshotRepository(session)


def market_event_repository(session: "AsyncSession") -> MarketEventRepository:
    """Return a market event repository bound to *session*."""

    return MarketEventRepository(session)


def audit_log_repository(session: "AsyncSession") -> AuditLogRepository:
    """Return an audit log repository bound to *session*."""

    return AuditLogRepository(session)
