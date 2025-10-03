"""Helpers for configuring the unified data layer."""

from __future__ import annotations

import os
from enum import Enum
from typing import Iterable, Mapping, Optional

__all__ = [
    "DataLayerMode",
    "DEFAULT_DATA_LAYER_MODE",
    "DEFAULT_CANDLE_INTERVALS",
    "DEFAULT_CANDLE_HISTORY_LIMIT",
    "get_data_layer_mode",
    "get_candle_intervals",
    "is_legacy_mode",
    "is_unified_mode",
    "is_dual_mode",
]


class DataLayerMode(str, Enum):
    """Available operating modes for the data layer."""

    LEGACY = "legacy"
    UNIFIED = "unified"
    DUAL = "dual"

    @classmethod
    def from_value(cls, value: Optional[str]) -> "DataLayerMode":
        if not value:
            return cls.LEGACY
        normalized = value.strip().lower()
        for member in cls:
            if member.value == normalized:
                return member
        return cls.LEGACY


DEFAULT_DATA_LAYER_MODE = DataLayerMode.from_value(os.getenv("DATA_LAYER_MODE"))

DEFAULT_CANDLE_INTERVALS: tuple[str, ...] = (
    "1s",
    "5s",
    "1m",
    "5m",
    "15m",
    "30m",
    "1h",
    "1d",
)
DEFAULT_CANDLE_HISTORY_LIMIT = 300


def get_data_layer_mode(
    *,
    env: Optional[Mapping[str, str]] = None,
    default: DataLayerMode = DEFAULT_DATA_LAYER_MODE,
) -> DataLayerMode:
    """Return the current data layer mode.

    Parameters
    ----------
    env:
        Optional mapping used to resolve environment overrides.  Supplying a
        custom mapping makes the function easier to unit test.
    default:
        Fallback mode when ``DATA_LAYER_MODE`` is not set or invalid.
    """

    if env is None:
        value = os.getenv("DATA_LAYER_MODE")
    else:
        value = env.get("DATA_LAYER_MODE")
    if value is None:
        return default
    return DataLayerMode.from_value(value)


def get_candle_intervals(
    *,
    env: Optional[Mapping[str, str]] = None,
    default_intervals: Iterable[str] = DEFAULT_CANDLE_INTERVALS,
    default_history: int = DEFAULT_CANDLE_HISTORY_LIMIT,
) -> tuple[list[str], int]:
    """Return configured candle intervals and history limit.

    Parameters
    ----------
    env:
        Optional mapping for resolving environment overrides.  Supplying a
        mapping simplifies unit testing.
    default_intervals:
        Fallback iterable of interval strings when no override is provided.
    default_history:
        Default number of candles to retain per interval when no override is
        supplied or the override is invalid.
    """

    if env is None:
        env_map: Mapping[str, str] = os.environ
    else:
        env_map = env

    raw_intervals = env_map.get("DATA_LAYER_CANDLE_INTERVALS")
    if raw_intervals:
        intervals = [
            part.strip().lower()
            for part in raw_intervals.split(",")
            if part.strip()
        ]
    else:
        intervals = [interval.strip().lower() for interval in default_intervals]

    if not intervals:
        intervals = [interval.strip().lower() for interval in default_intervals]

    history_raw = env_map.get("DATA_LAYER_CANDLE_HISTORY_LIMIT") or env_map.get(
        "DATA_LAYER_CANDLE_HISTORY"
    )
    history = default_history
    if history_raw:
        try:
            candidate = int(str(history_raw).strip())
        except (TypeError, ValueError):
            candidate = None
        if candidate and candidate > 0:
            history = candidate

    if history <= 0:
        history = default_history

    return intervals, history


def is_legacy_mode(mode: Optional[DataLayerMode] = None) -> bool:
    mode = mode or get_data_layer_mode()
    return mode is DataLayerMode.LEGACY


def is_unified_mode(mode: Optional[DataLayerMode] = None) -> bool:
    mode = mode or get_data_layer_mode()
    return mode is DataLayerMode.UNIFIED


def is_dual_mode(mode: Optional[DataLayerMode] = None) -> bool:
    mode = mode or get_data_layer_mode()
    return mode is DataLayerMode.DUAL

