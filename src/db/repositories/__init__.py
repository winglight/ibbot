"""High level repository factories for DB-API persistence."""

from __future__ import annotations

from .audit import AuditLogRepository, AuditLogRepositoryError
from .config import ConfigRepository, ConfigRepositoryError, ModuleConfigSnapshot
from .events import MarketEventRepository, MarketEventRepositoryError
from .order import OrderRepository, OrderRepositoryError
from .risk import RiskSnapshotRepository, RiskSnapshotRepositoryError
from .base import RepositoryError, RepositoryValidationError

__all__ = [
    "AuditLogRepository",
    "AuditLogRepositoryError",
    "ConfigRepository",
    "ConfigRepositoryError",
    "MarketEventRepository",
    "MarketEventRepositoryError",
    "OrderRepository",
    "OrderRepositoryError",
    "ModuleConfigSnapshot",
    "RiskSnapshotRepository",
    "RiskSnapshotRepositoryError",
    "RepositoryError",
    "RepositoryValidationError",
]
