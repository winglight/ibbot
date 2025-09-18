import math
from datetime import datetime

import pytest
from zoneinfo import ZoneInfo

from src.dom_parameter_tuner import (
    DEFAULT_BASE_THRESHOLDS,
    DOMParameterTuner,
)


def _to_timestamp_ns(dt: datetime) -> int:
    return int(dt.timestamp() * 1_000_000_000)


def test_session_detection_switches_thresholds():
    tuner = DOMParameterTuner()
    eastern = ZoneInfo("America/New_York")
    rth_ts = _to_timestamp_ns(datetime(2024, 9, 16, 11, 0, tzinfo=eastern))
    eth_ts = _to_timestamp_ns(datetime(2024, 9, 16, 20, 0, tzinfo=eastern))

    metrics_rth = {
        "obi5": 0.62,
        "ofi": 6.0,
        "cvd": 4.0,
        "trend_score": 0.4,
        "timestamp_ns": rth_ts,
    }
    metrics_eth = {
        "obi5": 0.55,
        "ofi": 1.0,
        "cvd": 0.5,
        "trend_score": 0.1,
        "timestamp_ns": eth_ts,
    }

    thresholds_rth = tuner.update(metrics_rth)
    assert thresholds_rth["session"]["active"] == "RTH"
    thresholds_eth = tuner.update(metrics_eth)
    assert thresholds_eth["session"]["active"] == "ETH"
    assert not math.isclose(
        thresholds_rth["long"]["obi"], thresholds_eth["long"]["obi"], rel_tol=1e-5
    )


def test_penalty_for_false_long_signals():
    tuner = DOMParameterTuner()
    base_long_obi = DEFAULT_BASE_THRESHOLDS["long"]["obi"]
    ts = _to_timestamp_ns(datetime(2024, 9, 16, 10, 0, tzinfo=ZoneInfo("America/New_York")))
    for i in range(5):
        payload = {
            "obi5": 0.75,
            "ofi": 1.0,
            "cvd": -3.0,
            "trend_score": -0.3,
            "timestamp_ns": ts + i * 1_000_000_000,
        }
        tuner.update(payload)
    adjusted = tuner.thresholds["long"]["obi"]
    assert adjusted > base_long_obi


def test_optimize_with_history_adjusts_base_thresholds():
    tuner = DOMParameterTuner()
    history = [
        {"obi5": 0.62, "ofi": 6.0, "outcome": 1},
        {"obi5": 0.66, "ofi": 7.0, "outcome": 1},
        {"obi5": 0.58, "ofi": 2.0, "outcome": 0},
    ]
    result = tuner.optimize_with_history(
        history, obi_candidates=[0.55, 0.6, 0.65], ofi_candidates=[0.0, 5.0]
    )
    assert result["long"]["obi"] in {0.6, 0.65}
    assert result["short"]["ofi"] == -result["long"]["ofi"]
