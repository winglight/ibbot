"""Public API for DB-API based repositories and utilities."""

from __future__ import annotations

from .async_support import run_in_thread
from .bootstrap import BootstrapResult, SchemaBootstrapError, ensure_schema
from .config import DatabaseConfigError, resolve_database_url
from .mariadb import (
    MariaDBConfigurationError,
    connect,
    get_database_url,
    get_service_metrics_columns,
    run_sql_query,
    set_engine,
)
from .schema import REQUIRED_TABLES
from .repositories import (
    AuditLogRepository,
    AuditLogRepositoryError,
    MarketEventRepository,
    MarketEventRepositoryError,
    MarketPriceRepository,
    MarketPriceRepositoryError,
    OrderRepository,
    OrderRepositoryError,
    RepositoryError,
    RepositoryValidationError,
    RiskSnapshotRepository,
    RiskSnapshotRepositoryError,
)

__all__ = [
    "BootstrapResult",
    "DatabaseConfigError",
    "MariaDBConfigurationError",
    "RepositoryError",
    "RepositoryValidationError",
    "AuditLogRepository",
    "AuditLogRepositoryError",
    "MarketEventRepository",
    "MarketEventRepositoryError",
    "MarketPriceRepository",
    "MarketPriceRepositoryError",
    "OrderRepository",
    "OrderRepositoryError",
    "SchemaBootstrapError",
    "RiskSnapshotRepository",
    "RiskSnapshotRepositoryError",
    "audit_log_writer",
    "connect",
    "get_database_url",
    "get_service_metrics_columns",
    "market_event_writer",
    "market_price_writer",
    "order_repository",
    "ensure_schema",
    "REQUIRED_TABLES",
    "resolve_database_url",
    "risk_snapshot_repository",
    "run_in_thread",
    "run_sql_query",
    "set_engine",
]


def order_repository(*, logger_name: str = "db.orders") -> OrderRepository:
    """Return an :class:`OrderRepository` configured with *logger_name*."""

    return OrderRepository(logger_name=logger_name)


def market_price_writer(*, logger_name: str = "db.market") -> MarketPriceRepository:
    """Return a :class:`MarketPriceRepository` instance."""

    return MarketPriceRepository(logger_name=logger_name)


def risk_snapshot_repository(*, logger_name: str = "db.risk") -> RiskSnapshotRepository:
    """Return a :class:`RiskSnapshotRepository` instance."""

    return RiskSnapshotRepository(logger_name=logger_name)


def market_event_writer(*, logger_name: str = "db.events") -> MarketEventRepository:
    """Return a :class:`MarketEventRepository` instance."""

    return MarketEventRepository(logger_name=logger_name)


def audit_log_writer(*, logger_name: str = "db.audit") -> AuditLogRepository:
    """Return a :class:`AuditLogRepository` instance."""

    return AuditLogRepository(logger_name=logger_name)
