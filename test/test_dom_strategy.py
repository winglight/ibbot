import os

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

    def check_circuit_breaker_before_new_order(self):
        return True, "ok"


class FakeOrderManager:
    def __init__(self):
        self.orders = []

    def record(self, payload):
        self.orders.append(payload)


@pytest.fixture
def strategy_setup():
    dom_service = FakeDOMService()
    order_manager = FakeOrderManager()
    queue = FakeQueueManager(order_manager=order_manager)
    risk_manager = FakeRiskManager()
    rule = RiskRule(symbol="MNQ", stop_loss_price=14980.0)
    risk_manager.risk_rules["MNQ"] = rule

    strategy = DOMStrategy(cooldown_seconds=0, trend_threshold=0.85)
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

    metrics = {"trend_score": 0.9, "mid_price": 15000.0, "symbol": "MNQ"}
    dom_service.emit("MNQ", metrics)

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

    dom_service.emit("MNQ", {"trend_score": 0.9, "mid_price": 15000.0, "symbol": "MNQ"})
    assert len(queue.tasks) == 0
    assert order_manager.orders == []


def test_dom_strategy_stop_prevents_orders(strategy_setup):
    strategy, dom_service, queue, _, order_manager = strategy_setup

    strategy.disable()
    dom_service.emit("MNQ", {"trend_score": 0.9, "mid_price": 15000.0, "symbol": "MNQ"})
    assert len(queue.tasks) == 0
    assert order_manager.orders == []


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

    strategy = DOMStrategy(symbol="MNQ", trend_threshold=0.9, cooldown_seconds=20, max_loss_streak=4)
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
    assert params.get("cooldown_seconds") == 20
    assert params.get("max_loss_streak") == 4
    assert pytest.approx(params.get("trend_threshold")) == 0.9
    assert params.get("default_quantity") == strategy.default_quantity
