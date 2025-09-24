import asyncio
import os
import time

import pytest

os.environ.setdefault("IBBOT_SKIP_SERVER", "1")

from src.risk_manager import RiskRule
from src.strategies.dom_strategy import DOMStrategy
from src.strategy_manager import StrategyManager


class FakeDOMService:
    def __init__(self, thresholds=None):
        self.trend_thresholds = thresholds or [0.5, 0.7, 0.85, 0.95]
        self._listeners = []

    def add_listener(self, callback):
        self._listeners.append(callback)

    def remove_listener(self, callback):
        if callback in self._listeners:
            self._listeners.remove(callback)

    def emit(self, symbol, metrics):
        for callback in list(self._listeners):
            callback(symbol, metrics, {})


class FakeQueueManager:
    def __init__(self, order_manager=None):
        self.tasks = []
        self.order_manager = order_manager

    def add_task(self, task_type, payload, max_retries=3):
        self.tasks.append((task_type, payload))
        if self.order_manager is not None:
            self.order_manager.record(payload)
        return f"task-{len(self.tasks)}"


class FakeRiskManager:
    def __init__(self):
        self.risk_rules = {}
        self.equity_last = 100000
        self.risk_per_trade_r = 0.01
        self.point_value_map = {"MNQ": 2.0}
        self.cb_daily_r = 0
        self.cb_streak = 0
        self.circuit_active = False
        self.loss_streak = 0
        self.daily_loss_R = 0.0
        self.position_limit_default = 100
        self.position_limit_per_symbol = {}

    def check_circuit_breaker_before_new_order(self):
        return True, "ok"

    def get_position_limit(self, symbol: str):
        return self.position_limit_per_symbol.get(symbol, self.position_limit_default)


class FakeOrderManager:
    def __init__(self):
        self.orders = []

    def record(self, payload):
        self.orders.append(payload)


def _sample_long_metrics() -> dict:
    return {
        "mid_price": 15000.0,
        "symbol": "MNQ",
        "tick_size": 0.25,
        "imbalance": 0.68,
        "imb": 0.68,
        "z_stack_bid": 1.25,
        "z_ofi": 1.4,
        "support_band": {"low": 14998.0, "high": 15002.0},
        "absorb_up": True,
        "spread_ticks": 25.0,
    }


@pytest.fixture
def strategy_setup():
    dom_service = FakeDOMService()
    order_manager = FakeOrderManager()
    queue = FakeQueueManager(order_manager=order_manager)
    risk_manager = FakeRiskManager()
    rule = RiskRule(symbol="MNQ", stop_loss_price=14980.0)
    risk_manager.risk_rules["MNQ"] = rule

    strategy = DOMStrategy(cooldown_seconds=0)
    strategy.set_dependencies(
        dom_service=dom_service,
        queue_manager=queue,
        risk_manager=risk_manager,
        order_manager=order_manager,
    )
    strategy.enable()
    return strategy, dom_service, queue, risk_manager, order_manager


def test_dom_strategy_places_order_on_signal(strategy_setup):
    strategy, dom_service, queue, _, order_manager = strategy_setup

    dom_service.emit("MNQ", _sample_long_metrics())

    assert len(queue.tasks) == 1
    task_type, payload = queue.tasks[0]
    assert task_type == "strategy_order"
    assert payload["action"] == "BUY"
    assert payload["symbol"] == "MNQ"
    # risk budget: 100000 * 0.01 = 1000; stop distance = 20; point value = 2 -> qty = 25
    assert payload["qty"] == 25
    assert order_manager.orders and order_manager.orders[0] == payload


def test_dom_strategy_breaker_blocks_after_losses(strategy_setup):
    strategy, dom_service, queue, _, order_manager = strategy_setup

    # Trip breaker by recording losses
    strategy.register_trade_result(-100)
    strategy.register_trade_result(-50)
    strategy.register_trade_result(-25)
    assert strategy.breaker_tripped is True

    dom_service.emit("MNQ", _sample_long_metrics())
    assert len(queue.tasks) == 0
    assert order_manager.orders == []


def test_dom_strategy_stop_prevents_orders(strategy_setup):
    strategy, dom_service, queue, _, order_manager = strategy_setup

    strategy.disable()
    dom_service.emit("MNQ", _sample_long_metrics())
    assert len(queue.tasks) == 0
    assert order_manager.orders == []


def test_dom_strategy_respects_signal_frequency(strategy_setup):
    strategy, dom_service, queue, _, _ = strategy_setup

    strategy.signal_frequency_seconds = 0.5
    dom_service.emit("MNQ", _sample_long_metrics())
    assert len(queue.tasks) == 1

    dom_service.emit("MNQ", _sample_long_metrics())
    assert len(queue.tasks) == 1

    time.sleep(0.6)
    dom_service.emit("MNQ", _sample_long_metrics())
    assert len(queue.tasks) == 2


def test_internal_strategy_info_exposes_dom_summary(tmp_path, monkeypatch):
    from src import database as db_module

    db_path = tmp_path / "test_trading.db"
    db_manager = db_module.DatabaseManager(db_path)
    monkeypatch.setattr(db_module, "db_manager", db_manager, raising=False)
    monkeypatch.setattr(db_module, "get_db_manager", lambda db_path=db_path: db_manager, raising=False)

    from src import strategy_manager as strategy_module

    monkeypatch.setattr(strategy_module, "get_db_manager", lambda: db_manager, raising=False)

    manager = StrategyManager(data_dir=tmp_path / "data", strategy_dir=tmp_path / "strategies")

    rule = RiskRule(symbol="MNQ", stop_loss_price=14980.0, take_profit_price=15040.0)

    class StubRisk:
        def __init__(self):
            self.risk_rules = {"MNQ": rule}

    manager.set_dependencies(risk_manager=StubRisk(), queue_manager=None)

    strategy = DOMStrategy(
        symbol="MNQ",
        cooldown_seconds=20,
        max_loss_streak=4,
        structure_window_seconds=12.0,
        structure_tolerance_ticks=1.5,
        min_signal_conditions=4,
    )
    manager.register_internal_strategy(strategy)

    info = manager.get_internal_strategy_info(strategy.name)
    assert info
    assert info["strategy_type"] == "internal"
    assert info["description"] == strategy.description
    assert info.get("file_path", "").endswith("dom_strategy.py")

    risk_rules = info.get("risk_rules", {})
    assert "MNQ" in risk_rules
    assert pytest.approx(risk_rules["MNQ"].get("stop_loss_price")) == 14980.0

    params = info.get("risk_parameters", {})
    assert pytest.approx(params.get("cooldown_seconds")) == 20
    assert params.get("max_loss_streak") == 4
    assert params.get("trend_threshold") is None
    assert params.get("default_quantity") == strategy.default_quantity
    assert pytest.approx(params.get("structure_window_seconds")) == 12.0
    assert pytest.approx(params.get("structure_tolerance_ticks")) == 1.5
    assert params.get("min_signal_conditions") == 4
    assert pytest.approx(params.get("signal_frequency_seconds")) == 60.0

    configurable = info.get("configurable_parameters")
    assert isinstance(configurable, list) and configurable
    names = {item.get("name") for item in configurable}
    assert "signal_frequency_seconds" in names
    first_param = next(item for item in configurable if item.get("name") == "cooldown_seconds")
    assert first_param.get("min") == 0.0


def test_strategy_manager_parameter_updates(tmp_path, monkeypatch):
    from src import database as db_module

    db_path = tmp_path / "test_trading_params.db"
    db_manager = db_module.DatabaseManager(db_path)
    monkeypatch.setattr(db_module, "db_manager", db_manager, raising=False)
    monkeypatch.setattr(db_module, "get_db_manager", lambda db_path=db_path: db_manager, raising=False)

    from src import strategy_manager as strategy_module

    monkeypatch.setattr(strategy_module, "get_db_manager", lambda: db_manager, raising=False)

    manager = StrategyManager(data_dir=tmp_path / "data", strategy_dir=tmp_path / "strategies")
    risk = FakeRiskManager()
    risk.risk_rules["MNQ"] = RiskRule(symbol="MNQ", stop_loss_price=14980.0)
    manager.set_dependencies(risk_manager=risk, queue_manager=None)

    strategy = DOMStrategy(symbol="MNQ")
    manager.register_internal_strategy(strategy)

    assert pytest.approx(strategy.cooldown_seconds) == 15.0
    assert pytest.approx(strategy.signal_frequency_seconds) == 60.0

    asyncio.run(
        manager.update_strategy(
            strategy.name,
            {"parameters": {"cooldown_seconds": 45, "signal_frequency_seconds": 120}},
        )
    )

    assert pytest.approx(strategy.cooldown_seconds) == 45
    assert pytest.approx(strategy.signal_frequency_seconds) == 120
    assert manager.settings.get(f"{strategy.name}_param_cooldown_seconds") == 45

    details = manager.get_strategy(strategy.name)
    assert details
    param_map = {item.get("name"): item.get("value") for item in details.get("parameters", [])}
    assert pytest.approx(param_map["cooldown_seconds"]) == 45
    assert pytest.approx(param_map["signal_frequency_seconds"]) == 120
