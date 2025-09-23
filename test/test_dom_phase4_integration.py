import os
import sys
import time
import types
import shutil
from collections import defaultdict

import pytest


if 'ib_insync' not in sys.modules:  # provide minimal stub so DOMService can import
    fake_ib = types.ModuleType('ib_insync')

    class _DummyIB:  # pragma: no cover - simple stub
        def __init__(self, *args, **kwargs):
            pass

        def disconnect(self):  # pragma: no cover - defensive no-op
            return None

        def isConnected(self):  # pragma: no cover - return False by default
            return False

    fake_ib.IB = _DummyIB
    fake_ib.util = types.SimpleNamespace(startLoop=lambda *args, **kwargs: None)

    class _DummyExecutionFilter:  # pragma: no cover - stub type
        def __init__(self, *args, **kwargs):
            pass

    fake_ib.ExecutionFilter = _DummyExecutionFilter
    class _DummyStock:  # pragma: no cover - stub contract helper
        def __init__(self, *args, **kwargs):
            pass

    fake_ib.Stock = _DummyStock
    sys.modules['ib_insync'] = fake_ib


from src.dom_cache import DomCacheManager
from src.dom_optimizer import DOMOptimizationOrchestrator
from src.dom_service import DOMService
from src.dom_signal_tracker import SignalOutcomeTracker, resolve_outcome_config


class DummyDB:
    def __init__(self):
        self.storage = {}

    def save_dom_config(self, payload):
        self.storage['dom_config'] = payload

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
    service.db_manager = DummyDB()
    service._previous_signal_flags = defaultdict(lambda: {"long": False, "short": False})
    delay, threshold = resolve_outcome_config()
    service._outcome_tracker = SignalOutcomeTracker(
        cache,
        evaluation_delay_seconds=delay,
        price_move_threshold=threshold,
    )
    return service


def _push_update(service, symbol, ts_ms, bids, asks):
    ts_ns = int(ts_ms * 1_000_000)
    payload = {
        "symbol": symbol,
        "bids": bids,
        "asks": asks,
        "ts_ns": ts_ns,
    }
    service._on_depth_update(symbol, payload)


def _extract_signals(history):
    return [
        item
        for item in history.get('metrics', [])
        if item.get('signal_type') in {'long', 'short'}
    ]


@pytest.mark.parametrize('symbol', ['MNQ'])
def test_dom_phase4_full_loop(tmp_path, symbol):
    os.environ['DOM_SIGNAL_OUTCOME_DELAY_SECONDS'] = '1'
    os.environ['DOM_SIGNAL_OUTCOME_THRESHOLD'] = '0.001'
    service = _build_service(tmp_path)
    dummy_db = service.db_manager
    orchestrator = DOMOptimizationOrchestrator(
        service=service,
        cache=service._cache,
        db_manager=dummy_db,
    )
    orchestrator.reset_state()

    try:
        initial_thresholds = service.get_tuner_base_thresholds()
        base_ms = time.time() * 1000.0
        # Baseline snapshot
        _push_update(
            service,
            symbol,
            base_ms,
            bids=[(100.00, 100), (99.75, 80)],
            asks=[(100.25, 100), (100.50, 80)],
        )
        # Long signal expected to succeed
        _push_update(
            service,
            symbol,
            base_ms + 200,
            bids=[(100.00, 220), (99.75, 120)],
            asks=[(100.25, 60), (100.50, 40)],
        )
        # Price drift upwards (evaluates the long signal)
        _push_update(
            service,
            symbol,
            base_ms + 1_400,
            bids=[(100.40, 220), (100.15, 120)],
            asks=[(100.65, 60), (100.90, 40)],
        )
        # Reset book before next signal
        _push_update(
            service,
            symbol,
            base_ms + 1_600,
            bids=[(100.20, 120), (99.95, 90)],
            asks=[(100.45, 120), (100.70, 90)],
        )
        # Short signal expected to失败
        _push_update(
            service,
            symbol,
            base_ms + 2_100,
            bids=[(100.10, 60), (99.85, 40)],
            asks=[(100.35, 210), (100.60, 160)],
        )
        # 价格上行，令该空头信号失败
        _push_update(
            service,
            symbol,
            base_ms + 3_400,
            bids=[(100.90, 40), (100.65, 30)],
            asks=[(101.15, 200), (101.40, 150)],
        )

        history = service.get_history(symbol, include_snapshots=False)
        signals = _extract_signals(history)
        assert len(signals) == 2
        outcomes = sorted(signal['outcome'] for signal in signals)
        assert outcomes == [0, 1]
        assert all('signal_price' in signal for signal in signals)
        assert all('thresholds' in metric for metric in history.get('metrics', []))

        assert initial_thresholds['long']['obi'] == pytest.approx(0.6, rel=1e-3)
        assert initial_thresholds['long']['ofi'] == pytest.approx(0.0, abs=1e-6)

        result = orchestrator.run_optimization(
            symbol,
            min_signals=1,
            apply_updates=True,
            obi_candidates=[0.6, 0.65, 0.7, 0.75],
            ofi_candidates=[0.0, 10.0, 50.0],
        )

        assert result['applied'] is True
        assert result['insufficient_data'] is False
        assert result['evaluated_records'] == 2
        assert result['positive_signals'] == 1
        assert result['negative_signals'] == 1
        assert result['precision'] == pytest.approx(0.5)
        assert result['updated_thresholds']['long']['obi'] >= initial_thresholds['long']['obi']
        assert result['updated_thresholds']['long']['ofi'] >= initial_thresholds['long']['ofi']

        updated_thresholds = service.get_tuner_base_thresholds()
        assert updated_thresholds == result['updated_thresholds']
        assert dummy_db.storage.get('dom_config') is not None

        # Feed an additional snapshot and ensure thresholds propagate to metrics
        _push_update(
            service,
            symbol,
            base_ms + 5_000,
            bids=[(100.80, 200), (100.55, 140)],
            asks=[(101.05, 80), (101.30, 60)],
        )
        refreshed_history = service.get_history(symbol, include_snapshots=False)
        latest_metric = refreshed_history['metrics'][-1]
        assert latest_metric['thresholds']['long']['obi'] >= updated_thresholds['long']['obi']
        assert latest_metric['thresholds']['short']['ofi'] <= updated_thresholds['short']['ofi']

        status = orchestrator.get_status()
        assert status['status'] == 'succeeded'
        assert status['last_result']['symbol'] == symbol
    finally:
        orchestrator.shutdown()
        shutil.rmtree(service._cache.base_dir, ignore_errors=True)
        os.environ.pop('DOM_SIGNAL_OUTCOME_DELAY_SECONDS', None)
        os.environ.pop('DOM_SIGNAL_OUTCOME_THRESHOLD', None)
