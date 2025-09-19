import copy
import time
from datetime import timedelta

import pytest

from src.dom_cache import DomCacheManager
from src.dom_optimizer import DOMOptimizationOrchestrator, _now
from src.dom_parameter_tuner import DOMParameterTuner
from src.dom_service import DOMService


class DummyDB:
    def __init__(self):
        self.configs = {}

    def save_dom_config(self, settings):
        self.configs["dom_config"] = copy.deepcopy(settings)

    def load_dom_config(self):
        return {}

    def save_config(self, key, value, description=None):
        self.configs[key] = copy.deepcopy(value)

    def get_config(self, key, default=None):
        value = self.configs.get(key, default)
        return copy.deepcopy(value)


@pytest.fixture
def orchestrator(monkeypatch, tmp_path):
    dummy_db = DummyDB()
    monkeypatch.setattr("src.dom_service.get_db_manager", lambda: dummy_db)

    service = DOMService()
    service.db_manager = dummy_db
    service._cache = DomCacheManager(
        base_dir=str(tmp_path), retention_seconds=3600, save_interval_seconds=1
    )

    orchestrator = DOMOptimizationOrchestrator(
        service=service, cache=service._cache, db_manager=dummy_db
    )
    orchestrator.reset_state()
    try:
        yield orchestrator, service, dummy_db
    finally:
        orchestrator.shutdown()


def _append_metrics(cache, symbol, entries):
    for item in entries:
        snapshot = {"timestamp": item.get("timestamp")}
        cache.append(symbol, snapshot, item)


def test_optimizer_applies_thresholds_and_updates_state(orchestrator):
    orchestrator_inst, service, dummy_db = orchestrator
    base_ts = time.time() * 1000.0
    metrics = [
        {
            "timestamp": base_ts + 1,
            "obi5": 0.72,
            "ofi": 6.0,
            "signal_type": "long",
            "outcome": 1,
        },
        {
            "timestamp": base_ts + 2,
            "obi5": 0.74,
            "ofi": 8.0,
            "signal_type": "long",
            "outcome": 1,
        },
        {
            "timestamp": base_ts + 3,
            "obi5": 0.62,
            "ofi": 5.0,
            "signal_type": "long",
            "outcome": 0,
        },
        {
            "timestamp": base_ts + 4,
            "obi5": 0.55,
            "ofi": 2.0,
            "signal_type": "long",
            "outcome": 0,
        },
    ]
    _append_metrics(service._cache, "ES", metrics)

    result = orchestrator_inst.run_optimization(
        "ES",
        min_signals=2,
        obi_candidates=[0.6, 0.65, 0.7, 0.75],
        ofi_candidates=[0.0, 5.0],
    )

    assert result["applied"] is True
    assert result["positive_signals"] == 2
    assert result["negative_signals"] == 2
    assert result["insufficient_data"] is False

    updated_base = service.get_tuner_base_thresholds()
    assert updated_base["long"]["obi"] >= 0.65
    assert "dom_automation_state" in dummy_db.configs

    status = orchestrator_inst.get_status()
    assert status["status"] == "succeeded"
    assert status["is_running"] is False
    assert status["last_result"]["symbol"] == "ES"

    schedule_status = orchestrator_inst.configure_schedule(enabled=True, interval_minutes=60)
    assert schedule_status["schedule"]["enabled"] is True
    assert schedule_status["schedule"]["interval_minutes"] == 60
    assert schedule_status["next_run_at"] is not None

    disabled_status = orchestrator_inst.configure_schedule(enabled=False, interval_minutes=None)
    assert disabled_status["schedule"]["enabled"] is False
    assert disabled_status["next_run_at"] is None


def test_optimizer_reports_insufficient_data(orchestrator):
    orchestrator_inst, service, _ = orchestrator
    base_ts = time.time() * 1000.0
    metrics = [
        {
            "timestamp": base_ts + 1,
            "obi5": 0.55,
            "ofi": 1.0,
            "signal_type": "long",
            "outcome": None,
        }
    ]
    _append_metrics(service._cache, "NQ", metrics)

    result = orchestrator_inst.run_optimization("NQ", min_signals=1, apply_updates=False)

    assert result["applied"] is False
    assert result["insufficient_data"] is True
    assert result["evaluated_records"] == 0


def test_schedule_plan_configuration(orchestrator):
    orchestrator_inst, _, _ = orchestrator

    start_at = _now() - timedelta(minutes=5)
    status = orchestrator_inst.configure_schedule(
        enabled=True,
        interval_minutes=30,
        start_at=start_at,
        symbol="es",
        lookback_minutes=120,
        limit=200,
        apply_updates=False,
        min_signals=2,
        obi_candidates=[0.6, "0.7", None],
        ofi_candidates=[0, 5.5],
    )

    assert status["schedule"]["enabled"] is True
    assert status["schedule"]["interval_minutes"] == 30
    assert status["plan"]["symbol"] == "ES"
    assert status["plan"]["lookback_minutes"] == 120
    assert status["plan"]["limit"] == 200
    assert status["plan"]["apply_updates"] is False
    assert status["plan"]["min_signals"] == 2
    assert status["plan"]["obi_candidates"] == [0.6, 0.7]
    assert status["plan"]["ofi_candidates"] == [0.0, 5.5]
    assert status["status"] == "idle"

    disabled = orchestrator_inst.configure_schedule(enabled=False, interval_minutes=None)
    assert disabled["schedule"]["enabled"] is False
    assert disabled["status"] == "idle"
    assert disabled["scheduled_at"] is None


def test_run_scheduled_once(orchestrator):
    orchestrator_inst, service, _ = orchestrator
    base_ts = time.time() * 1000.0
    metrics = [
        {
            "timestamp": base_ts + 1,
            "obi5": 0.72,
            "ofi": 6.0,
            "signal_type": "long",
            "outcome": 1,
        },
        {
            "timestamp": base_ts + 2,
            "obi5": 0.64,
            "ofi": 5.2,
            "signal_type": "long",
            "outcome": 0,
        },
    ]
    _append_metrics(service._cache, "ES", metrics)

    orchestrator_inst.configure_schedule(
        enabled=True,
        interval_minutes=5,
        start_at=_now() - timedelta(minutes=1),
        symbol="ES",
        lookback_minutes=60,
        apply_updates=False,
        min_signals=1,
    )

    result = orchestrator_inst.run_scheduled_once(reference_time=_now())
    assert result is not None
    assert result["symbol"] == "ES"
    status = orchestrator_inst.get_status()
    assert status["status"] == "succeeded"
    assert status["scheduled_at"] is not None
    assert status["plan"]["symbol"] == "ES"
    assert status["schedule"]["next_run_at"] is not None


def test_run_optimization_failure_updates_state(orchestrator, monkeypatch):
    orchestrator_inst, service, _ = orchestrator
    base_ts = time.time() * 1000.0
    metrics = [
        {
            "timestamp": base_ts + 1,
            "obi5": 0.7,
            "ofi": 6.0,
            "signal_type": "long",
            "outcome": 1,
        }
    ]
    _append_metrics(service._cache, "ES", metrics)

    def raise_error(self, *args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(DOMParameterTuner, "optimize_with_history", raise_error)

    with pytest.raises(RuntimeError):
        orchestrator_inst.run_optimization("ES")

    status = orchestrator_inst.get_status()
    assert status["status"] == "failed"
    assert status["is_running"] is False
    assert status["last_error"] == "boom"
    assert status["last_duration_ms"] is not None
