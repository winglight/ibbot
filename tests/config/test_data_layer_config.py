"""Unit tests for data layer configuration helpers."""

from __future__ import annotations

import pytest

from src.data_layer.config import (
    DEFAULT_CANDLE_HISTORY_LIMIT,
    DEFAULT_CANDLE_INTERVALS,
    DataLayerMode,
    get_candle_intervals,
    get_data_layer_mode,
    is_dual_mode,
    is_legacy_mode,
    is_unified_mode,
)


def test_get_data_layer_mode_defaults_to_legacy():
    """Empty environment should fall back to the legacy mode."""

    mode = get_data_layer_mode(env={})

    assert mode is DataLayerMode.LEGACY


def test_get_data_layer_mode_respects_case_insensitive_env():
    """Environment overrides should be case-insensitive."""

    mode = get_data_layer_mode(env={"DATA_LAYER_MODE": "UnIfIeD"})

    assert mode is DataLayerMode.UNIFIED


def test_get_data_layer_mode_uses_default_for_invalid_values():
    """Invalid values should return the provided default fallback."""

    mode = get_data_layer_mode(
        env={"DATA_LAYER_MODE": "unsupported"},
        default=DataLayerMode.DUAL,
    )

    assert mode is DataLayerMode.DUAL


def test_get_data_layer_mode_reads_process_environment(monkeypatch: pytest.MonkeyPatch):
    """When no mapping is provided the OS environment should be consulted."""

    monkeypatch.setenv("DATA_LAYER_MODE", "dual")

    mode = get_data_layer_mode()

    assert mode is DataLayerMode.DUAL


def test_mode_helper_shortcuts_respect_explicit_values():
    """Helper predicates should use the provided mode without consulting globals."""

    assert is_legacy_mode(DataLayerMode.LEGACY) is True
    assert is_unified_mode(DataLayerMode.UNIFIED) is True
    assert is_dual_mode(DataLayerMode.DUAL) is True

    assert is_legacy_mode(DataLayerMode.UNIFIED) is False
    assert is_unified_mode(DataLayerMode.LEGACY) is False
    assert is_dual_mode(DataLayerMode.LEGACY) is False


def test_get_candle_intervals_defaults_to_constants():
    """When no overrides exist the defaults should be normalised and returned."""

    intervals, history = get_candle_intervals(env={})

    assert intervals == [interval.lower() for interval in DEFAULT_CANDLE_INTERVALS]
    assert history == DEFAULT_CANDLE_HISTORY_LIMIT


def test_get_candle_intervals_honours_environment_overrides(monkeypatch: pytest.MonkeyPatch):
    """Overrides should be trimmed, normalised, and validated."""

    monkeypatch.setenv("DATA_LAYER_CANDLE_INTERVALS", "1S, 10M , 2H,, ")
    monkeypatch.setenv("DATA_LAYER_CANDLE_HISTORY_LIMIT", "450")

    intervals, history = get_candle_intervals()

    assert intervals == ["1s", "10m", "2h"]
    assert history == 450


@pytest.mark.parametrize(
    "history_value",
    ["0", "-5", "not-a-number"],
)
def test_get_candle_intervals_rejects_invalid_history(history_value: str):
    """Invalid history overrides should fall back to the default limit."""

    intervals, history = get_candle_intervals(
        env={
            "DATA_LAYER_CANDLE_INTERVALS": "1s,5s",
            "DATA_LAYER_CANDLE_HISTORY_LIMIT": history_value,
        }
    )

    assert intervals == ["1s", "5s"]
    assert history == DEFAULT_CANDLE_HISTORY_LIMIT


def test_get_candle_intervals_supports_legacy_history_key(monkeypatch: pytest.MonkeyPatch):
    """The legacy history key should still be honoured for backwards compatibility."""

    monkeypatch.delenv("DATA_LAYER_CANDLE_HISTORY_LIMIT", raising=False)
    monkeypatch.setenv("DATA_LAYER_CANDLE_HISTORY", "123")

    intervals, history = get_candle_intervals()

    assert history == 123

    monkeypatch.delenv("DATA_LAYER_CANDLE_HISTORY", raising=False)
    # Ensure no interference with other tests in the session.
    for key in (
        "DATA_LAYER_CANDLE_INTERVALS",
        "DATA_LAYER_CANDLE_HISTORY_LIMIT",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def clear_data_layer_mode_env(monkeypatch: pytest.MonkeyPatch):
    """Ensure per-test isolation for the primary environment variables."""

    for key in (
        "DATA_LAYER_MODE",
        "DATA_LAYER_CANDLE_INTERVALS",
        "DATA_LAYER_CANDLE_HISTORY_LIMIT",
        "DATA_LAYER_CANDLE_HISTORY",
    ):
        monkeypatch.delenv(key, raising=False)

    yield

    for key in (
        "DATA_LAYER_MODE",
        "DATA_LAYER_CANDLE_INTERVALS",
        "DATA_LAYER_CANDLE_HISTORY_LIMIT",
        "DATA_LAYER_CANDLE_HISTORY",
    ):
        monkeypatch.delenv(key, raising=False)
