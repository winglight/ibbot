"""Utilities for deriving price scale information from IB contracts."""

from __future__ import annotations

from typing import Any, Dict, Tuple

import math

__all__ = ["resolve_price_scale"]


# Common futures multipliers are often required for translating account data
# into per-contract prices.  The IB API sometimes returns the multiplier as a
# string and certain micro contracts share multipliers with their full-sized
# counterparts.  These overrides normalise the values to something predictable.
_MULTIPLIER_OVERRIDES: Dict[Tuple[str, str], float] = {
    ("FUT", "MNQ"): 2.0,   # Micro E-mini Nasdaq-100
    ("FUT", "NQ"): 20.0,   # E-mini Nasdaq-100
    ("FUT", "MES"): 5.0,   # Micro E-mini S&P 500
    ("FUT", "ES"): 50.0,   # E-mini S&P 500
    ("FUT", "MYM"): 0.5,  # Micro E-mini Dow
    ("FUT", "YM"): 5.0,   # E-mini Dow
    ("FUT", "M2K"): 5.0,  # Micro E-mini Russell 2000
    ("FUT", "RTY"): 50.0, # E-mini Russell 2000
    ("FUT", "MCL"): 100.0,  # Micro WTI Crude Oil
    ("FUT", "CL"): 1000.0,  # WTI Crude Oil
    ("FUT", "MGC"): 10.0,   # Micro Gold
    ("FUT", "GC"): 100.0,   # Gold
}


def _coerce_multiplier(value: Any) -> float | None:
    """Best-effort conversion of a raw multiplier into a finite float."""

    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric) or numeric <= 0:
        return None
    return numeric


def resolve_price_scale(
    symbol: str | None,
    security_type: str | None,
    raw_multiplier: Any,
) -> tuple[float, float, str]:
    """Return the contract multiplier, price scale and provenance string.

    The price scale represents the factor that converts IBKR account values
    (which are often expressed in *notional* terms) into per-contract prices.
    For example, Micro futures typically report an ``avgCost`` that is twice
    the traded price.  Applying the derived ``price_scale`` (``0.5`` in that
    case) recovers the display price expected by the UI.
    """

    symbol_key = (security_type or "").upper(), (symbol or "").upper()

    override = _MULTIPLIER_OVERRIDES.get(symbol_key)
    if override is not None:
        multiplier = override
        source = f"rule.{symbol_key[0]}:{symbol_key[1]}"
    else:
        coerced = _coerce_multiplier(raw_multiplier)
        if coerced is not None:
            multiplier = coerced
            source = "contract"
        else:
            multiplier = 1.0
            source = "default"

    price_scale = 1.0
    if multiplier and math.isfinite(multiplier):
        price_scale = 1.0 / multiplier

    return multiplier, price_scale, source

