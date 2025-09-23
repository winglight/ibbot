import copy

import pytest

from src.dom_processor import DOMRingBuffer, DOMProcessor
from src.dom_parameter_tuner import DEFAULT_FEATURE_THRESHOLDS


class _StaticTuner:
    def __init__(self) -> None:
        feature_thresholds = copy.deepcopy(DEFAULT_FEATURE_THRESHOLDS)
        feature_thresholds["stacking"]["intensity"] = 0.0
        feature_thresholds["stacking"]["min_depth"] = 0.0
        feature_thresholds["absorption"]["index"] = 0.0
        feature_thresholds["sweep"]["intensity"] = 0.0
        feature_thresholds["fake_breakout"]["prob"] = 1.0
        feature_thresholds["obi_ofi_reversal"]["score"] = 1.1
        feature_thresholds["wall_pull_no_follow"]["score"] = 1.0
        self.thresholds = {
            "session": {"active": "RTH"},
            "long": {"obi": 0.8, "ofi": 0.0},
            "short": {"obi": 0.2, "ofi": 0.0},
            "absorb": {"obi_buy": 0.4, "obi_sell": 0.6, "ofi_sign": 0.0},
            "sweep": {"ofi_multiplier": 2.0, "min_abs_ofi": 0.0},
            "features": feature_thresholds,
        }

    def update(self, metrics, events=()):
        return self.thresholds


def test_ring_buffer_wrap():
    buf = DOMRingBuffer(window=3, max_levels=2)
    # add 4 snapshots to force wrap
    for i in range(4):
        bids = [(100 - i, 10 + i)]
        asks = [(101 + i, 20 + i)]
        buf.append_snapshot(bids, asks, mid_tick=1000 + i, ts_ns=i)
    assert buf._filled == 3
    # latest snapshot corresponds to i=3
    idx = (buf._idx - 1) % buf.window
    assert buf.mid_tick[idx] == 1003
    assert buf.valid_levels[idx, 0] == 1
    assert buf.valid_levels[idx, 1] == 1


def test_calc_obi_and_ofi():
    p = DOMProcessor(window=10, max_levels=5)
    # first snapshot
    p.on_depth_update({
        "bids": [(100, 10), (99, 8)],
        "asks": [(101, 7), (102, 6)],
    })
    # OFI should be zero for first snapshot
    assert p.last_metrics["ofi"] == 0.0
    obi_expected = 18 / (18 + 13)
    assert pytest.approx(p.last_metrics["obi5"], rel=1e-5) == obi_expected

    # second snapshot with changed sizes
    p.on_depth_update({
        "bids": [(100, 12), (99, 8)],
        "asks": [(101, 6), (102, 6)],
    })
    obi_expected = 20 / (20 + 12)
    assert pytest.approx(p.last_metrics["obi5"], rel=1e-5) == obi_expected
    # OFI uses top level size diffs with mid unchanged
    expected_ofi = (12 - 10) - (6 - 7)
    assert p.last_metrics["ofi"] == expected_ofi

    # third snapshot where mid price increases -> only bid diff counts
    p.on_depth_update({
        "bids": [(101, 13), (100, 8)],
        "asks": [(102, 4), (103, 6)],
    })
    expected_ofi = 13 - 12  # ask diff ignored as mid moved up
    assert p.last_metrics["ofi"] == expected_ofi
    assert isinstance(p.last_metrics.get("feature_flags"), dict)
    assert isinstance(p.last_metrics.get("events"), list)


def test_compute_features_and_cvd():
    p = DOMProcessor(window=5, max_levels=1)
    p.on_depth_update({"bids": [(100, 10)], "asks": [(101, 20)]})
    # first snapshot -> cvd=0
    assert p.last_metrics["cvd"] == 0.0

    p.on_depth_update({"bids": [(100, 11)], "asks": [(101, 15)]})
    m = p.last_metrics
    assert m["cvd"] == 6.0
    assert pytest.approx(m["zscore_obi5"], rel=1e-5) == 1.0
    assert 0.0 <= m["fake_breakout_prob"] <= 1.0
    assert "feature_flags" in m and isinstance(m["feature_flags"], dict)


def test_mid_price_preserves_scale():
    p = DOMProcessor(window=5, max_levels=1)
    p.on_depth_update({"bids": [(4321.25, 5)], "asks": [(4321.75, 7)]})
    m = p.last_metrics
    assert pytest.approx(m["mid_price"], rel=1e-6) == 4321.5
    idx = (p.buffer._idx - 1) % p.buffer.window
    stored_bid = int(p.buffer.book[idx, 0, 0, 0])
    stored_ask = int(p.buffer.book[idx, 1, 0, 0])
    assert stored_bid == int(round(4321.25 * 100))
    assert stored_ask == int(round(4321.75 * 100))


def test_detect_events_absorb():
    p = DOMProcessor(window=5, max_levels=2)
    p.on_depth_update({
        "bids": [(100, 20), (99, 18)],
        "asks": [(101, 50), (102, 30)],
    })
    p.on_depth_update({
        "bids": [(100, 28), (99, 18)],
        "asks": [(101, 40), (102, 30)],
    })
    events, details = p.detect_events(p.features_history)
    assert "absorption_buy" in events
    assert any(evt.get("key") == "absorption_buy" for evt in details)
    assert p.last_metrics["feature_flags"]["absorption_buy"]


def test_trend_score_range():
    p = DOMProcessor(window=5, max_levels=1)
    p.on_depth_update({"bids": [(100, 10)], "asks": [(101, 20)]})
    p.on_depth_update({"bids": [(100, 15)], "asks": [(101, 10)]})
    score = p.last_metrics.get("trend_score")
    assert score is not None
    assert -1.0 <= score <= 1.0
    assert score > 0


def test_stacking_feature_flags():
    p = DOMProcessor(window=5, max_levels=3)
    p.on_depth_update({
        "bids": [(100, 20), (99, 15), (98, 10)],
        "asks": [(101, 25), (102, 20), (103, 15)],
    })
    p.on_depth_update({
        "bids": [(100, 80), (99, 40), (98, 20)],
        "asks": [(101, 20), (102, 15), (103, 10)],
    })
    flags = p.last_metrics["feature_flags"]
    assert flags["stacking_buy"]
    assert any(evt.get("code") == "K" for evt in p.last_metrics.get("events", []))


def test_dom_processor_respects_custom_thresholds():
    tuner = _StaticTuner()
    p = DOMProcessor(window=5, max_levels=1, parameter_tuner=tuner)
    p.on_depth_update({
        "bids": [(100, 10)],
        "asks": [(101, 20)],
        "ts_ns": 1_000_000_000,
    })
    assert not p.last_signals["long"]
    p.on_depth_update({
        "bids": [(100, 80)],
        "asks": [(101, 10)],
        "ts_ns": 2_000_000_000,
    })
    assert p.last_signals["long"]
