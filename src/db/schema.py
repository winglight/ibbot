"""Schema helpers for the ibbot MariaDB repositories."""

from __future__ import annotations

from typing import FrozenSet

__all__ = ["REQUIRED_TABLES"]

REQUIRED_TABLES: FrozenSet[str] = frozenset(
    {
        "orders",
        "risk_rules",
        "strategies",
        "config",
    }
)
