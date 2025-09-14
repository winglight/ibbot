import pytest

from src.dom_processor import DOMRingBuffer, DOMProcessor


def test_ring_buffer_wrap():
    buf = DOMRingBuffer(window=3, max_levels=2)
    # add 4 snapshots to force wrap
    for i in range(4):
        bids = [(100 - i, 10 + i)]
        asks = [(101 + i, 20 + i)]
        buf.append(bids, asks, mid_tick=1000 + i, ts_ns=i)
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
    obi_expected = (18 - 13) / 31
    assert pytest.approx(p.last_metrics["obi5"], rel=1e-5) == obi_expected

    # second snapshot with changed sizes
    p.on_depth_update({
        "bids": [(100, 12), (99, 8)],
        "asks": [(101, 6), (102, 6)],
    })
    obi_expected = (20 - 12) / 32
    assert pytest.approx(p.last_metrics["obi5"], rel=1e-5) == obi_expected
    # OFI uses top level size diffs
    expected_ofi = (12 - 10) - (6 - 7)
    assert p.last_metrics["ofi"] == expected_ofi
