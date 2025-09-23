"""Provides static metadata for optimizer configuration."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, List


DEFAULT_BASE_METRICS: List[str] = ["obi5", "ofi", "trend_score", "cvd", "mid_price"]
DEFAULT_FEATURE_METRICS: List[str] = [
    "stacking",
    "pulling",
    "wall",
    "cancel_rate",
    "absorption",
    "sweep",
    "fake_breakout",
    "wall_pull_no_follow",
    "obi_ofi_reversal",
    "spread",
]
DEFAULT_ALGORITHMS: List[str] = ["grid", "random", "bayesian"]


def get_available_base_metrics() -> List[str]:
    return list(DEFAULT_BASE_METRICS)


def get_available_feature_metrics() -> List[str]:
    return list(DEFAULT_FEATURE_METRICS)


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
