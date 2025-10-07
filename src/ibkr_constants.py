"""
IBKR Trading Constants

This module contains constants and utilities that were previously part of IBKRClient
but are still needed by other modules after the architecture refactor.
"""

from typing import Dict, Tuple
from datetime import datetime, timedelta

# Index future contract details
INDEX_FUTURE_DETAILS: Dict[str, Dict[str, str]] = {
    "ES": {"exchange": "CME", "multiplier": "50", "tradingClass": "ES"},
    "MES": {"exchange": "CME", "multiplier": "5", "tradingClass": "MES"},
    "NQ": {"exchange": "CME", "multiplier": "20", "tradingClass": "NQ"},
    "MNQ": {"exchange": "CME", "multiplier": "2", "tradingClass": "MNQ"},
    "YM": {"exchange": "ECBOT", "multiplier": "5", "tradingClass": "YM"},
    "MYM": {"exchange": "ECBOT", "multiplier": "0.5", "tradingClass": "MYM"},
    "RTY": {"exchange": "CME", "multiplier": "50", "tradingClass": "RTY"},
    "M2K": {"exchange": "CME", "multiplier": "5", "tradingClass": "M2K"},
    "VX": {"exchange": "CFE", "multiplier": "1000", "tradingClass": "VX"},
}

INDEX_FUTURE_SYMBOLS = frozenset(INDEX_FUTURE_DETAILS.keys())
_QUARTERLY_CONTRACT_MONTHS = (3, 6, 9, 12)


def _third_friday_datetime(year: int, month: int) -> datetime:
    """Calculate the third Friday of a given month and year."""
    # Find the first day of the month
    first_day = datetime(year, month, 1)
    # Find the first Friday (weekday 4)
    first_friday = first_day + timedelta(days=(4 - first_day.weekday()) % 7)
    # Add 14 days to get the third Friday
    third_friday = first_friday + timedelta(days=14)
    return third_friday


def _front_quarter_contract(now: datetime) -> Tuple[int, int]:
    """Determine the front quarter contract month and year."""
    current_year = now.year
    current_month = now.month
    
    # Find the next quarterly month
    for month in _QUARTERLY_CONTRACT_MONTHS:
        if month >= current_month:
            # Check if we're past the third Friday of this month
            third_friday = _third_friday_datetime(current_year, month)
            if now.date() <= third_friday.date():
                return current_year, month
    
    # If we're past all quarterly months this year, use March of next year
    return current_year + 1, 3