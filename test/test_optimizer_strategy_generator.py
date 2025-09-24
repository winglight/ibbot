"""Tests for DOM optimizer strategy generation output."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Callable

import pytest

from src.strategies.dom_strategy import DOMStrategy


class DummyDomService:
    """Minimal DOM service stub recording listeners."""

    def __init__(self) -> None:
        self.listeners: list[Callable[[Any], None]] = []

    def add_listener(self, callback: Callable[[Any], None]) -> None:
        self.listeners.append(callback)

    def remove_listener(self, callback: Callable[[Any], None]) -> None:
        try:
            self.listeners.remove(callback)
        except ValueError:  # pragma: no cover - defensive cleanup
            pass


@pytest.mark.parametrize(
    "base_params, feature_params",
    [
        (
            {"long_threshold": 0.8, "short_threshold": -0.5, "window": 5000},
            {"stacking": {"intensity": 0.4}, "wall": {"strength": 0.6}},
        ),
    ],
)
def test_generated_strategy_uses_dom_strategy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base_params: dict[str, Any], feature_params: dict[str, Any]) -> None:
    from src.optimizer_service import strategy_generator

    # Redirect generator output into an isolated temporary directory.
    monkeypatch.setattr(strategy_generator, "STRATEGY_DIR", tmp_path)

    strategy_name = "Test DOM Optimizer"
    file_path_str, class_name = strategy_generator.generate_strategy_file(
        strategy_name=strategy_name,
        base_parameters=base_params,
        feature_parameters=feature_params,
        plan_id="plan-123",
        task_id="task-456",
    )

    generated_path = Path(file_path_str)
    assert generated_path.exists(), "Generated strategy file should exist"

    module_name = "generated_strategy_module"
    spec = importlib.util.spec_from_file_location(module_name, generated_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[arg-type]

    strategy_cls = getattr(module, class_name)
    assert issubclass(strategy_cls, DOMStrategy)

    strategy_instance = strategy_cls()
    dom_service = DummyDomService()
    strategy_instance.set_dependencies(dom_service=dom_service)

    # Ensure DOMStrategy behaviour wires the listener on start/stop.
    started = strategy_instance.start()
    assert started is True
    assert len(dom_service.listeners) == 1

    config = strategy_instance.get_configuration()
    assert config["base"] == base_params
    assert config["features"] == feature_params

    strategy_instance.stop()
    assert dom_service.listeners == []
