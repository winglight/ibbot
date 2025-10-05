"""Runtime configuration helpers for DOM optimisation components."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

__all__ = [
    "DEFAULT_OUTCOME_DELAY_SECONDS",
    "DEFAULT_OUTCOME_THRESHOLD",
    "DOMLoggingSettings",
    "DOMOutcomeSettings",
    "get_logging_settings",
    "get_outcome_settings",
    "is_parameter_tuner_enabled",
]


TRUE_VALUES = {"1", "true", "yes", "on"}

DEFAULT_OUTCOME_DELAY_SECONDS = 600.0
DEFAULT_OUTCOME_THRESHOLD = 0.005

DOM_VERBOSE_LOGGING_ENV = "DOM_VERBOSE_LOGGING"
DOM_LOG_LEVEL_ENV = "DOM_LOG_LEVEL"
DOM_OUTCOME_DELAY_ENV = "DOM_SIGNAL_OUTCOME_DELAY_SECONDS"
DOM_OUTCOME_THRESHOLD_ENV = "DOM_SIGNAL_OUTCOME_THRESHOLD"
DOM_DISABLE_PARAMETER_TUNER_ENV = "DOM_DISABLE_PARAMETER_TUNER"


@dataclass(frozen=True)
class DOMOutcomeSettings:
    """Outcome evaluation timing configuration."""

    evaluation_delay_seconds: float
    price_move_threshold: float


@dataclass(frozen=True)
class DOMLoggingSettings:
    """Logging verbosity toggles for DOM components."""

    verbose_penalty_logs: bool
    log_level: str


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return bool(default)
    return value.strip().lower() in TRUE_VALUES


def _env_int(name: str, default: Optional[int], *, minimum: Optional[int] = None) -> Optional[int]:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None and parsed < minimum:
        return default
    return parsed


def _env_float(name: str, default: float, *, minimum: Optional[float] = None) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        result = default
    else:
        try:
            result = float(value)
        except (TypeError, ValueError):
            result = default
    if minimum is not None:
        result = max(result, minimum)
    return result


def _env_symbol(name: str) -> Optional[str]:
    value = os.getenv(name)
    if not value:
        return None
    cleaned = value.strip().upper()
    return cleaned or None


def get_outcome_settings() -> DOMOutcomeSettings:
    """Return the current outcome evaluation configuration."""

    delay = _env_float(
        DOM_OUTCOME_DELAY_ENV,
        DEFAULT_OUTCOME_DELAY_SECONDS,
        minimum=1.0,
    )
    threshold = _env_float(
        DOM_OUTCOME_THRESHOLD_ENV,
        DEFAULT_OUTCOME_THRESHOLD,
        minimum=0.0,
    )
    return DOMOutcomeSettings(
        evaluation_delay_seconds=delay,
        price_move_threshold=threshold,
    )


def get_logging_settings() -> DOMLoggingSettings:
    """Return logging configuration for DOM modules."""

    verbose = _env_flag(DOM_VERBOSE_LOGGING_ENV, False)
    level = os.getenv(DOM_LOG_LEVEL_ENV, "INFO").strip().upper()
    return DOMLoggingSettings(
        verbose_penalty_logs=verbose,
        log_level=level or "INFO",
    )


def is_parameter_tuner_enabled() -> bool:
    """Return whether the adaptive DOM parameter tuner should be enabled."""

    return not _env_flag(DOM_DISABLE_PARAMETER_TUNER_ENV, False)


