from __future__ import annotations

from src.data_layer.config import DEFAULT_CANDLE_INTERVALS, get_candle_intervals


def test_default_candle_intervals_include_extended_durations() -> None:
    intervals, history = get_candle_intervals(
        env={}, default_intervals=DEFAULT_CANDLE_INTERVALS
    )
    assert history > 0
    normalized_defaults = [interval.strip().lower() for interval in DEFAULT_CANDLE_INTERVALS]
    assert intervals == normalized_defaults
    for expected in ("15m", "30m", "1h", "1d"):
        assert expected in intervals


def test_get_candle_intervals_normalises_environment_values() -> None:
    env = {"DATA_LAYER_CANDLE_INTERVALS": "1S, 5M, 15M, 1H, 1D"}
    intervals, _ = get_candle_intervals(env=env, default_intervals=())
    assert intervals == ["1s", "5m", "15m", "1h", "1d"]
