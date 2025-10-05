"""Schema helpers for the ibbot MariaDB repositories."""

from __future__ import annotations

from typing import FrozenSet

__all__ = ["REQUIRED_TABLES"]

REQUIRED_TABLES: FrozenSet[str] = frozenset(
    {
        "orders",
        "market_prices",
        "risk_config_snapshots",
        "market_events",
        "risk_rules",
        "strategies",
        "historical_data",
        "config",
        "sizing_history",
        "notification_history",
    }
)
