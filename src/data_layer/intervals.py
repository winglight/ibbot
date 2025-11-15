"""Interval aggregation utilities for converting ticks into candles."""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
import time
from typing import Dict, Iterable, List, Tuple

__all__ = [
    "IntervalTransformer",
]

_INTERVAL_PATTERN = re.compile(r"^(\d+)([smhd])$")


def _parse_interval(value: str) -> int:
    match = _INTERVAL_PATTERN.match(value.strip().lower())
    if not match:
        raise ValueError(f"Unsupported interval: {value}")
    amount, unit = match.groups()
    amount_i = int(amount)
    if unit == "s":
        return amount_i
    if unit == "m":
        return amount_i * 60
    if unit == "h":
        return amount_i * 3600
    if unit == "d":
        return amount_i * 86400
    raise ValueError(f"Unsupported interval unit: {unit}")


@dataclass
class _CandleState:
    interval_seconds: int
    start_ns: int
    end_ns: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    count: int

    def update(self, price: float, volume: float, ts_ns: int) -> None:
        self.close = price
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.volume += volume
        self.count += 1
        self.end_ns = max(self.end_ns, ts_ns)

    def to_dict(self, *, interval: str, is_closed: bool) -> Dict[str, float]:
        return {
            "interval": interval,
            "interval_start_ns": self.start_ns,
            "interval_end_ns": self.end_ns,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "count": self.count,
            "is_closed": is_closed,
        }


class IntervalTransformer:
    """Maintain rolling candle state for multiple intervals."""

    def __init__(self, intervals: Iterable[str]) -> None:
        interval_map = {}
        for interval in intervals:
            seconds = _parse_interval(interval)
            interval_map[interval] = seconds
        if not interval_map:
            raise ValueError("IntervalTransformer requires at least one interval")
        self._intervals = interval_map
        self._states: Dict[str, Dict[str, _CandleState]] = {}

    def reset(self, symbol: str) -> None:
        self._states.pop(symbol, None)

    def update(
        self,
        symbol: str,
        *,
        price: float,
        volume: float,
        ts_ns: int,
    ) -> List[Tuple[str, Dict[str, float]]]:
        symbol_key = symbol.upper()
        states = self._states.setdefault(symbol_key, {})
        emissions: List[Tuple[str, Dict[str, float]]] = []

        for interval, seconds in self._intervals.items():
            bucket_ns = (ts_ns // (seconds * 1_000_000_000)) * (seconds * 1_000_000_000)
            state = states.get(interval)
            if state and bucket_ns > state.start_ns:
                emissions.append((interval, state.to_dict(interval=interval, is_closed=True)))
                state = None

            if state is None:
                start_ns = bucket_ns
                end_ns = bucket_ns + seconds * 1_000_000_000
                state = _CandleState(
                    interval_seconds=seconds,
                    start_ns=start_ns,
                    end_ns=start_ns,
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    volume=volume,
                    count=1,
                )
                states[interval] = state
            else:
                state.update(price, volume, ts_ns)

            emissions.append((interval, state.to_dict(interval=interval, is_closed=False)))

        return emissions

