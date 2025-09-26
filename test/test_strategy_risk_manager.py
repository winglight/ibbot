import asyncio

import pytest

from src.strategy_risk import (
    DEFAULT_STRATEGY_SETTINGS,
    StrategyPosition,
    StrategyRiskManager,
)


class DummyDB:
    def __init__(self, stored=None):
        self.config = stored or {}

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def save_config(self, key, value, description=None):
        self.config[key] = value


def test_string_position_limit_is_enforced():
    db = DummyDB(
        {
            "strategy_risk_settings": {
                "alpha": {
                    "max_positions": "1",
                    "forbid_add_on_loss": "false",
                }
            }
        }
    )
    manager = StrategyRiskManager(db_manager=db)

    async def fake_get_positions(strategy_name):
        return [StrategyPosition(symbol="ES", quantity=1.0, unrealized_pnl=0.0)]

    manager._get_strategy_positions = fake_get_positions  # type: ignore[assignment]

    settings = manager.get_settings("alpha")
    assert settings["max_positions"] == 1
    assert settings["forbid_add_on_loss"] is False

    allowed, reason = asyncio.run(
        manager.evaluate_order(
            strategy_name="alpha",
            symbol="NQ",
            action="BUY",
            quantity=1.0,
            current_position=0.0,
            new_position=1.0,
        )
    )

    assert not allowed
    assert "上限" in (reason or "")


def test_settings_normalization_handles_strings():
    db = DummyDB(
        {
            "strategy_risk_settings": {
                "beta": {
                    "loss_time_threshold": "120",
                    "loss_pnl_threshold": "-50.5",
                    "notify_on_trigger": "0",
                }
            }
        }
    )
    manager = StrategyRiskManager(db_manager=db)

    normalized = manager.get_settings("beta")
    assert normalized["loss_time_threshold"] == pytest.approx(120.0)
    assert normalized["loss_pnl_threshold"] == pytest.approx(-50.5)
    assert normalized["notify_on_trigger"] is False
    assert normalized["forbid_add_on_loss"] == DEFAULT_STRATEGY_SETTINGS["forbid_add_on_loss"]
