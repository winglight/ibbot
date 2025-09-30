import json

import pytest

from services.risk.engine import RiskEngine


@pytest.fixture
def engine() -> RiskEngine:
    engine = RiskEngine()
    config = {
        "rules": json.dumps(
            [
                {"symbol": "MNQ", "stop_loss": 100.0, "take_profit": 120.0},
                {"symbol": "MES", "stop_loss": 50.0, "enabled": False},
            ]
        )
    }
    engine.load_config(config)
    return engine


def test_stop_loss_triggers_once(engine: RiskEngine) -> None:
    events = engine.evaluate_price("mnq", 99.5, {"last": 99.5})
    assert len(events) == 1
    event = events[0]
    assert event.trigger == "stop_loss"
    assert engine.is_armed("mnq", "stop_loss") is False

    # Trigger should remain locked until reset
    assert engine.evaluate_price("MNQ", 98.0, {"last": 98.0}) == []
    engine.reset_trigger("MNQ", "stop_loss")
    assert engine.is_armed("MNQ", "stop_loss") is True


def test_take_profit_trigger(engine: RiskEngine) -> None:
    engine.reset_trigger("MNQ", "stop_loss")
    events = engine.evaluate_price("MNQ", 125.0, {"last": 125.0})
    assert len(events) == 1
    assert events[0].trigger == "take_profit"


def test_disabled_rule_does_not_trigger(engine: RiskEngine) -> None:
    assert engine.evaluate_price("MES", 40.0, {"last": 40.0}) == []
