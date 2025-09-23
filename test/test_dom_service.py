import copy

import pytest

from src.dom_parameter_tuner import DEFAULT_BASE_THRESHOLDS, DEFAULT_FEATURE_THRESHOLDS
from src.dom_service import DOMService
from src.dom_processor import DOMProcessor


def test_get_metrics_only_trend_score():
    service = DOMService()
    service.processor = DOMProcessor()
    # Manually set metrics and signals to ensure only metrics are exposed
    service.processor.last_metrics = {"trend_score": 0.42, "obi5": 0.5}
    service.processor.last_signals = {"long": True, "short": False}
    metrics = service.get_metrics()
    assert "trend_score" in metrics
    assert "long" not in metrics and "short" not in metrics


def test_dom_service_tuner_env_configuration(monkeypatch):
    monkeypatch.setenv("DOM_TUNER_STATS_WINDOW", "1200")
    monkeypatch.setenv("DOM_TUNER_PENALTY_DECAY", "0.9")
    monkeypatch.setenv("DOM_TUNER_PENALTY_STEP", "0.05")
    monkeypatch.setenv("DOM_TUNER_MAX_PENALTY", "0.3")
    monkeypatch.setenv("DOM_TUNER_REVERSAL_WINDOW_SEC", "120")
    monkeypatch.setenv("DOM_TUNER_SESSION_TZ", "UTC")
    monkeypatch.setattr("src.dom_service.get_db_manager", lambda: None)

    service = DOMService()
    tuner = service.processor.parameter_tuner

    assert tuner.penalty_step == pytest.approx(0.05)
    assert tuner.penalty_decay == pytest.approx(0.9)
    assert tuner.max_penalty == pytest.approx(0.3)
    assert tuner.reversal_window_ns == 120 * 1_000_000_000
    assert service.get_config()["tuner_parameters"]["stats_window"] == 1200
    assert service.get_config()["tuner_parameters"]["session_timezone"] == "UTC"
    assert service.get_config()["tuner_feature_thresholds"] == DEFAULT_FEATURE_THRESHOLDS


def test_dom_service_persisted_tuner_config_overrides_env(monkeypatch):
    monkeypatch.setenv("DOM_TUNER_STATS_WINDOW", "600")

    class DummyDB:
        def load_dom_config(self):
            return {
                "window": 1500,
                "max_levels": 25,
                "trend_thresholds": [0.4, 0.6, 0.8, 0.9],
                "tuner_base_thresholds": copy.deepcopy(DEFAULT_BASE_THRESHOLDS),
                "tuner_feature_thresholds": copy.deepcopy(DEFAULT_FEATURE_THRESHOLDS),
                "tuner_parameters": {
                    "stats_window": 1800,
                    "penalty_decay": 0.85,
                    "penalty_step": 0.03,
                    "max_penalty": 0.25,
                    "reversal_window_sec": 90,
                    "session_timezone": "Europe/London",
                },
            }

        def save_dom_config(self, payload):
            self.saved = payload

    dummy_db = DummyDB()
    monkeypatch.setattr("src.dom_service.get_db_manager", lambda: dummy_db)

    service = DOMService()
    tuner = service.processor.parameter_tuner

    assert tuner.penalty_step == pytest.approx(0.03)
    assert tuner.penalty_decay == pytest.approx(0.85)
    assert tuner.max_penalty == pytest.approx(0.25)
    assert tuner.reversal_window_ns == 90 * 1_000_000_000
    assert service.get_config()["tuner_parameters"]["stats_window"] == 1800
    assert service.get_config()["tuner_parameters"]["session_timezone"] == "Europe/London"
    assert service.get_config()["tuner_feature_thresholds"] == DEFAULT_FEATURE_THRESHOLDS
