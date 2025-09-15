import pytest

from src.dom_processor import DOMRingBuffer, DOMProcessor


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


def test_compute_features_and_cvd():
    p = DOMProcessor(window=5, max_levels=1)
    p.on_depth_update({"bids": [(100, 10)], "asks": [(101, 20)]})
    # first snapshot -> cvd=0
    assert p.last_metrics["cvd"] == 0.0

    p.on_depth_update({"bids": [(100, 11)], "asks": [(101, 15)]})
    m = p.last_metrics
    assert m["cvd"] == 6.0
    assert pytest.approx(m["zscore_obi5"], rel=1e-5) == 1.0


def test_detect_events_absorb():
    p = DOMProcessor(window=5, max_levels=1)
    p.on_depth_update({"bids": [(100, 10)], "asks": [(101, 20)]})
    p.on_depth_update({"bids": [(100, 11)], "asks": [(101, 18)]})
    events = p.detect_events(p.features_history)
    assert "buy_absorb" in events


def test_trend_score_range():
    p = DOMProcessor(window=5, max_levels=1)
    p.on_depth_update({"bids": [(100, 10)], "asks": [(101, 20)]})
    p.on_depth_update({"bids": [(100, 15)], "asks": [(101, 10)]})
    score = p.last_metrics.get("trend_score")
    assert score is not None
    assert -1.0 <= score <= 1.0
    assert score > 0
