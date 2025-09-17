import os

import pytest

os.environ.setdefault("IBBOT_SKIP_SERVER", "1")

from src.risk_manager import RiskRule
from src.strategies.dom_strategy import DOMStrategy


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
    def __init__(self):
        self.tasks = []

    def add_task(self, task_type, payload, max_retries=3):
        self.tasks.append((task_type, payload))
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


@pytest.fixture
def strategy_setup():
    dom_service = FakeDOMService()
    queue = FakeQueueManager()
    risk_manager = FakeRiskManager()
    rule = RiskRule(symbol="MNQ", stop_loss_price=14980.0)
    risk_manager.risk_rules["MNQ"] = rule

    strategy = DOMStrategy(cooldown_seconds=0, trend_threshold=0.85)
    strategy.set_dependencies(
        dom_service=dom_service,
        queue_manager=queue,
        risk_manager=risk_manager,
    )
    strategy.enable()
    return strategy, dom_service, queue, risk_manager


def test_dom_strategy_places_order_on_signal(strategy_setup):
    strategy, dom_service, queue, risk_manager = strategy_setup

    metrics = {"trend_score": 0.9, "mid_price": 15000.0, "symbol": "MNQ"}
    dom_service.emit("MNQ", metrics)

    assert len(queue.tasks) == 1
    task_type, payload = queue.tasks[0]
    assert task_type == "strategy_order"
    assert payload["action"] == "BUY"
    assert payload["symbol"] == "MNQ"
    # risk budget: 100000 * 0.01 = 1000; stop distance = 20; point value = 2 -> qty = 25
    assert payload["qty"] == 25


def test_dom_strategy_breaker_blocks_after_losses(strategy_setup):
    strategy, dom_service, queue, _ = strategy_setup

    # Trip breaker by recording losses
    strategy.register_trade_result(-100)
    strategy.register_trade_result(-50)
    strategy.register_trade_result(-25)
    assert strategy.breaker_tripped is True

    dom_service.emit("MNQ", {"trend_score": 0.9, "mid_price": 15000.0, "symbol": "MNQ"})
    assert len(queue.tasks) == 0


def test_dom_strategy_stop_prevents_orders(strategy_setup):
    strategy, dom_service, queue, _ = strategy_setup

    strategy.stop()
    dom_service.emit("MNQ", {"trend_score": 0.9, "mid_price": 15000.0, "symbol": "MNQ"})
    assert len(queue.tasks) == 0
