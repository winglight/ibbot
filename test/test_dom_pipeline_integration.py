import time
from collections import defaultdict

from src.dom_cache import DomCacheManager
from src.dom_optimizer import DOMOptimizationOrchestrator
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


class DummyDB:
    def __init__(self):
        self.storage = {}

    def save_dom_config(self, payload):
        self.storage["dom_config"] = payload

    def load_dom_config(self):
        return {}

    def save_config(self, key, value, description=None):
        self.storage[key] = value

    def get_config(self, key, default=None):
        return self.storage.get(key, default)


def _build_service(tmp_path):
    service = DOMService()
    cache = DomCacheManager(
        base_dir=str(tmp_path), retention_seconds=3600, save_interval_seconds=1
    )
    service._cache = cache
    service.processor = DummyProcessor()
    service._previous_signal_flags = defaultdict(lambda: {"long": False, "short": False})
    service._outcome_tracker = SignalOutcomeTracker(
        cache,
        evaluation_delay_seconds=1,
        price_move_threshold=0.005,
    )
    return service


def _push_update(service, symbol, ts_ms, mid_price, signals, obi, ofi):
    ts_ns = int(ts_ms * 1_000_000)
    metrics = {
        "timestamp": ts_ms,
        "timestamp_ns": ts_ns,
        "mid_price": mid_price,
        "obi5": obi,
        "ofi": ofi,
    }
    data = {
        "bids": [(mid_price, 1)],
        "asks": [(mid_price + 0.25, 1)],
        "ts_ns": ts_ns,
        "test_metrics": metrics,
        "test_signals": signals,
    }
    service._on_depth_update(symbol, data)


def test_dom_pipeline_integration(tmp_path):
    symbol = "MNQ"
    service = _build_service(tmp_path)
    dummy_db = DummyDB()
    service.db_manager = dummy_db

    base_ts = time.time() * 1000.0
    _push_update(
        service,
        symbol,
        base_ts,
        mid_price=100.0,
        signals={"long": True, "short": False},
        obi=0.75,
        ofi=7.5,
    )
    _push_update(
        service,
        symbol,
        base_ts + 1_500,
        mid_price=101.2,
        signals={"long": False, "short": False},
        obi=0.52,
        ofi=1.0,
    )
    _push_update(
        service,
        symbol,
        base_ts + 3_000,
        mid_price=102.0,
        signals={"long": False, "short": True},
        obi=0.32,
        ofi=-4.0,
    )
    _push_update(
        service,
        symbol,
        base_ts + 4_800,
        mid_price=103.0,
        signals={"long": False, "short": False},
        obi=0.48,
        ofi=-1.5,
    )

    history = service.get_history(symbol, include_snapshots=False)
    signal_entries = [
        item
        for item in history.get("metrics", [])
        if item.get("signal_type") in {"long", "short"}
    ]
    assert len(signal_entries) == 2
    outcomes = {entry["signal_type"]: entry.get("outcome") for entry in signal_entries}
    assert outcomes == {"long": 1, "short": 0}

    orchestrator = DOMOptimizationOrchestrator(
        service=service,
        cache=service._cache,
        db_manager=dummy_db,
    )
    try:
        result = orchestrator.run_optimization(
            symbol,
            min_signals=2,
            apply_updates=False,
            obi_candidates=[0.6, 0.7],
            ofi_candidates=[0.0, 5.0],
        )

        assert result["evaluated_records"] == 2
        assert result["positive_signals"] == 1
        assert result["negative_signals"] == 1
        assert result["pending_signals"] == 0
        assert result["insufficient_data"] is False
        assert result["updated_thresholds"]
        status = orchestrator.get_status()
        assert status["status"] == "succeeded"
        assert status["last_result"]["symbol"] == symbol
    finally:
        orchestrator.shutdown()
