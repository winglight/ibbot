from __future__ import annotations

import copy
import types
from typing import Any, Dict, List

import pytest

import src.dom_service as dom_service_module
from src.dom_dependencies import DOMConfig
from src.dom_optimizer import DOMOptimizationOrchestrator
from src.dom_processor import DOMProcessor


@pytest.fixture()
def isolated_dom_service(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(dom_service_module.atexit, "register", lambda *args, **kwargs: None)

    dom_config = DOMConfig()
    monkeypatch.setattr(dom_service_module, "dom_config", dom_config)

    processor = DOMProcessor(window=dom_config.window, max_levels=dom_config.max_levels)
    monkeypatch.setattr(dom_service_module, "get_dom_processor", lambda: processor)
    monkeypatch.setattr(dom_service_module, "set_dom_processor", lambda _: None)

    yield


def test_dom_service_loads_persisted_config(monkeypatch: pytest.MonkeyPatch, isolated_dom_service: None) -> None:
    persisted = {
        "window": 4321,
        "max_levels": 12,
        "trend_thresholds": [0.55, 0.7, 0.9, 0.95],
        "tuner_base_thresholds": copy.deepcopy(dom_service_module.DEFAULT_BASE_THRESHOLDS),
        "tuner_feature_thresholds": copy.deepcopy(dom_service_module.DEFAULT_FEATURE_THRESHOLDS),
        "tuner_parameters": {
            "stats_window": 250,
            "penalty_decay": 0.97,
            "penalty_step": 0.03,
            "max_penalty": 0.18,
            "reversal_window_sec": 45,
            "session_timezone": "UTC",
        },
    }

    monkeypatch.setattr(dom_service_module, "load_dom_config", lambda: persisted)
    monkeypatch.setattr(dom_service_module, "save_dom_config", lambda payload: None)

    service = dom_service_module.DOMService()
    cfg = service.get_config()

    assert cfg["window"] == 4321
    assert cfg["max_levels"] == 12
    assert cfg["trend_thresholds"] == [0.55, 0.7, 0.9, 0.95]
    assert cfg["tuner_parameters"]["stats_window"] == 250


def test_dom_service_persists_config(monkeypatch: pytest.MonkeyPatch, isolated_dom_service: None) -> None:
    saved_payloads: List[Dict[str, Any]] = []

    monkeypatch.setattr(dom_service_module, "load_dom_config", lambda: None)

    def _capture(payload: Dict[str, Any]) -> None:
        saved_payloads.append(payload)

    monkeypatch.setattr(dom_service_module, "save_dom_config", _capture)

    service = dom_service_module.DOMService()
    service.update_config(2048, 22, [0.4, 0.6, 0.8, 0.95])

    assert saved_payloads, "DOM configuration was not persisted"
    persisted = saved_payloads[-1]
    assert persisted["window"] == 2048
    assert persisted["max_levels"] == 22
    assert persisted["trend_thresholds"] == [0.4, 0.6, 0.8, 0.95]


def test_dom_optimizer_uses_persistence_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    loaded: Dict[str, Any] = {
        "schedule": {"enabled": True, "interval_minutes": 60},
        "plan": {"symbol": "ES", "lookback_minutes": 120},
    }
    load_calls: List[Dict[str, Any]] = []
    saved_payloads: List[Dict[str, Any]] = []

    def _loader() -> Dict[str, Any]:
        load_calls.append(loaded)
        return loaded

    def _persister(payload: Dict[str, Any]) -> None:
        saved_payloads.append(payload)

    monkeypatch.setattr(
        DOMOptimizationOrchestrator,
        "_start_scheduler",
        lambda self: None,
    )

    fake_service = types.SimpleNamespace(_cache=None)
    orchestrator = DOMOptimizationOrchestrator(
        service=fake_service,
        state_loader=_loader,
        state_persister=_persister,
    )

    assert load_calls, "State loader was not invoked"
    status = orchestrator.get_status()
    assert status["plan"]["symbol"] == "ES"

    orchestrator.reset_state()
    assert saved_payloads, "State persister was not invoked"
    orchestrator.shutdown()
