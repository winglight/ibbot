import os
import time
from collections import defaultdict

import pytest

from src.dom_cache import DomCacheManager
from src.dom_service import DOMService
from src.dom_signal_tracker import SignalOutcomeTracker


class DummyProcessor:
    def __init__(self):
        self.last_metrics = {}
        self.last_signals = {"long": False, "short": False}
        self.last_events = []

    def on_depth_update(self, data):
        metrics = data.get("test_metrics", {})
        signals = data.get("test_signals", {"long": False, "short": False})
        self.last_metrics = dict(metrics)
        self.last_signals = dict(signals)
        self.last_events = data.get("test_events", [])


@pytest.fixture
def cache(tmp_path):
    return DomCacheManager(base_dir=str(tmp_path), retention_seconds=3600, save_interval_seconds=1)


def _append_initial_metric(cache, symbol, metrics):
    snapshot = {"timestamp": metrics.get("timestamp")}
    cache.append(symbol, snapshot, metrics)


def test_tracker_marks_long_success(cache):
    tracker = SignalOutcomeTracker(cache, evaluation_delay_seconds=60, price_move_threshold=0.01)
    symbol = "ES"
    base_ts = time.time() * 1000.0
    entry_price = 100.0
    metrics = {"timestamp": base_ts, "mid_price": entry_price}

    registration = tracker.register_signal(symbol, "long", metrics)
    assert registration is not None
    metrics_with_signal = dict(metrics)
    metrics_with_signal.update(registration)
    _append_initial_metric(cache, symbol, metrics_with_signal)

    future_metrics = {
        "timestamp": base_ts + 60_000 + 500,
        "mid_price": entry_price * 1.02,
    }
    updates = tracker.evaluate_on_metrics(symbol, future_metrics)
    assert updates and updates[0]["outcome"] == 1

    history = cache.get_history(symbol)
    stored = history["metrics"][0]
    assert stored["outcome"] == 1
    assert stored["signal"] == "long"
    assert stored["outcome_price"] == pytest.approx(future_metrics["mid_price"])


def test_tracker_marks_short_failure(cache):
    tracker = SignalOutcomeTracker(cache, evaluation_delay_seconds=120, price_move_threshold=0.01)
    symbol = "NQ"
    base_ts = time.time() * 1000.0
    entry_price = 200.0
    metrics = {"timestamp": base_ts, "mid_price": entry_price}

    registration = tracker.register_signal(symbol, "short", metrics)
    assert registration is not None
    metrics_with_signal = dict(metrics)
    metrics_with_signal.update(registration)
    _append_initial_metric(cache, symbol, metrics_with_signal)

    future_metrics = {
        "timestamp": base_ts + 120_000,
        "mid_price": entry_price * 1.005,
    }
    tracker.evaluate_on_metrics(symbol, future_metrics)

    stored = cache.get_history(symbol)["metrics"][0]
    assert stored["outcome"] == 0
    assert stored["signal"] == "short"


def test_dom_service_registers_and_resolves_outcome(tmp_path):
    os.environ["DOM_SIGNAL_OUTCOME_DELAY_SECONDS"] = "60"
    os.environ["DOM_SIGNAL_OUTCOME_THRESHOLD"] = "0.01"
    service = DOMService()

    try:
        cache = DomCacheManager(base_dir=str(tmp_path), retention_seconds=3600, save_interval_seconds=1)
        service._cache = cache
        service._outcome_tracker = SignalOutcomeTracker(cache, evaluation_delay_seconds=60, price_move_threshold=0.01)
        service._previous_signal_flags = defaultdict(lambda: {"long": False, "short": False})
        service.processor = DummyProcessor()

        base_ts_ms = time.time() * 1000.0
        base_ts_ns = int(base_ts_ms * 1_000_000)
        entry_price = 150.0
        metrics = {
            "timestamp": base_ts_ms,
            "timestamp_ns": base_ts_ns,
            "mid_price": entry_price,
            "obi5": 0.7,
            "ofi": 6.0,
        }
        data = {
            "bids": [(entry_price, 1)],
            "asks": [(entry_price + 0.25, 1)],
            "ts_ns": base_ts_ns,
            "test_metrics": metrics,
            "test_signals": {"long": True, "short": False},
        }
        service._on_depth_update("ES", data)

        history = cache.get_history("ES")
        assert history["metrics"][-1]["signal_type"] == "long"
        assert history["metrics"][-1]["signal"] == "long"
        signal_id = history["metrics"][-1]["signal_id"]

        later_ts_ms = base_ts_ms + 60_000 + 1_000
        later_ts_ns = int(later_ts_ms * 1_000_000)
        later_metrics = {
            "timestamp": later_ts_ms,
            "timestamp_ns": later_ts_ns,
            "mid_price": entry_price * 1.03,
            "obi5": 0.4,
            "ofi": 0.0,
        }
        data2 = {
            "bids": [(entry_price * 1.03, 1)],
            "asks": [(entry_price * 1.03 + 0.25, 1)],
            "ts_ns": later_ts_ns,
            "test_metrics": later_metrics,
            "test_signals": {"long": False, "short": False},
        }
        service._on_depth_update("ES", data2)

        final_history = cache.get_history("ES")
        stored = next(item for item in final_history["metrics"] if item.get("signal_id") == signal_id)
        assert stored["outcome"] == 1
    finally:
        os.environ.pop("DOM_SIGNAL_OUTCOME_DELAY_SECONDS", None)
        os.environ.pop("DOM_SIGNAL_OUTCOME_THRESHOLD", None)


def test_dom_service_signal_queries(tmp_path, monkeypatch):
    monkeypatch.setattr("src.dom_service.get_db_manager", lambda: None)
    service = DOMService()
    service._cache = DomCacheManager(
        base_dir=str(tmp_path), retention_seconds=3600, save_interval_seconds=1
    )

    base_ts = time.time() * 1000.0
    metrics = [
        {
            "timestamp": base_ts + 1,
            "signal_type": "long",
            "obi5": 0.72,
            "ofi": 6.0,
            "mid_price": 100.0,
            "outcome": 1,
        },
        {
            "timestamp": base_ts + 2,
            "signal_type": "short",
            "obi5": 0.45,
            "ofi": -7.0,
            "mid_price": 99.5,
            "outcome": 0,
        },
        {
            "timestamp": base_ts + 3,
            "signal_type": "long",
            "obi5": 0.6,
            "ofi": 4.0,
            "mid_price": 101.0,
            "outcome": 0,
        },
        {
            "timestamp": base_ts + 4,
            "signal_type": "short",
            "obi5": 0.5,
            "ofi": -4.0,
            "mid_price": 98.5,
            "outcome": None,
        },
    ]

    for item in metrics:
        service._cache.append("ES", {"timestamp": item["timestamp"]}, item)

    summary = service.summarize_signals("ES")
    assert summary["total_signals"] == 4
    assert summary["long_signals"] == 2
    assert summary["short_signals"] == 2
    assert summary["hits"] == 1
    assert summary["failures"] == 2
    assert summary["pending"] == 1

    filtered_summary = service.summarize_signals(
        "ES",
        direction="long",
        thresholds={"long": {"obi_min": 0.65}},
    )
    assert filtered_summary["total_signals"] == 1
    assert filtered_summary["long_signals"] == 1
    assert filtered_summary["short_signals"] == 0

    pending_query = service.query_signals(
        "ES",
        thresholds={"outcome": "pending"},
    )
    assert pending_query["total"] == 1
    assert pending_query["items"][0]["outcome"] is None

    paged_query = service.query_signals(
        "ES",
        direction="short",
        page=1,
        page_size=1,
    )
    assert paged_query["total"] == 2
    assert paged_query["total_pages"] == 2
    assert paged_query["items"][0]["signal_type"] == "short"
