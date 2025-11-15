"""Helpers for configuring the unified data layer."""

from __future__ import annotations

import os
from enum import Enum
from typing import Mapping, Optional

__all__ = [
    "DataLayerMode",
    "DEFAULT_DATA_LAYER_MODE",
    "get_data_layer_mode",
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


def is_legacy_mode(mode: Optional[DataLayerMode] = None) -> bool:
    mode = mode or get_data_layer_mode()
    return mode is DataLayerMode.LEGACY


def is_unified_mode(mode: Optional[DataLayerMode] = None) -> bool:
    mode = mode or get_data_layer_mode()
    return mode is DataLayerMode.UNIFIED


def is_dual_mode(mode: Optional[DataLayerMode] = None) -> bool:
    mode = mode or get_data_layer_mode()
    return mode is DataLayerMode.DUAL

