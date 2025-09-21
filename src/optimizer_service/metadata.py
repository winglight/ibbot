"""Provides static metadata for optimizer configuration."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, List


DEFAULT_METRICS: List[str] = ["obi", "ofi", "trend"]
DEFAULT_ALGORITHMS: List[str] = ["grid", "random", "bayesian"]


def get_available_metrics() -> List[str]:
    return list(DEFAULT_METRICS)


def get_available_algorithms() -> List[str]:
    return list(DEFAULT_ALGORITHMS)


def get_symbol_data_range(symbol: str) -> Dict[str, str]:
    today = date.today()
    start = today - timedelta(days=60)
    return {
        "symbol": symbol,
        "start_date": start.isoformat(),
        "end_date": today.isoformat(),
        "records": 60 * 24 * 2,  # placeholder resolution: 2 per hour
    }
