import os
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("IBBOT_SKIP_SERVER", "1")

from src.strategy_manager import StrategyManager, Strategy  # noqa: E402


class DummyScheduler:
    def __init__(self):
        self.jobs = []
        self.running = False

    def start(self):
        self.running = True

    def remove_all_jobs(self):
        self.jobs.clear()

    def add_job(self, func, trigger, *, hour, minute, id, args, replace_existing, coalesce, misfire_grace_time, max_instances):
        self.jobs.append(
            {
                "func": func,
                "trigger": trigger,
                "hour": hour,
                "minute": minute,
                "id": id,
                "args": args,
                "replace_existing": replace_existing,
                "coalesce": coalesce,
                "misfire_grace_time": misfire_grace_time,
                "max_instances": max_instances,
            }
        )


class DummyQueueManager:
    def __init__(self):
        self.tasks = []

    def add_task(self, task_type, payload):
        self.tasks.append((task_type, payload))


class DummyDBManager:
    def __init__(self, strategy_path: Path):
        self.strategy_path = strategy_path
        self.saved_config = None

    def load_strategy_config(self):
        return {
            "api_endpoint": "/api/strategy",
            "api_method": "POST",
            "api_payload_template": {},
            "strategy_path": str(self.strategy_path),
            "enable_auth": False,
            "auth_token": None,
        }

    def save_strategy_config(self, config):
        self.saved_config = config

    def load_strategies(self):
        return []


@pytest.fixture()
def strategy_manager(monkeypatch, tmp_path):
    strategy_dir = tmp_path / "strategies"
    db_manager = DummyDBManager(strategy_dir)
    monkeypatch.setattr("src.strategy_manager.get_db_manager", lambda: db_manager)

    manager = StrategyManager(data_dir=str(tmp_path / "data"), strategy_dir=str(strategy_dir))
    manager.scheduler = DummyScheduler()
    manager.queue_manager = DummyQueueManager()
    return manager


def test_schedule_strategy_orders_registers_jobs(strategy_manager):
    strat = Strategy(
        name="test_strategy",
        strategy_type="internal",
        enabled=True,
        schedule=[
            {"time": "14:30", "symbol": "AAPL", "action": "BUY", "qty": 2},
            {"time": "09:45", "symbol": "MSFT", "action": "SELL", "qty": 1, "type": "LIMIT", "price": 300.5},
        ],
    )
    strategy_manager.strategies[strat.name] = strat

    strategy_manager.schedule_strategy_orders()

    assert len(strategy_manager.scheduler.jobs) == 2
    first_job = strategy_manager.scheduler.jobs[0]
    assert first_job["trigger"] == 'cron'
    assert first_job["hour"] == 14
    assert first_job["minute"] == 30
    payload = first_job["args"][0]
    assert payload["strategy"] == "test_strategy"
    assert payload["symbol"] == "AAPL"
    assert payload["qty"] == 2

    second_job = strategy_manager.scheduler.jobs[1]
    assert second_job["hour"] == 9
    assert second_job["minute"] == 45
    payload2 = second_job["args"][0]
    assert payload2["type"] == "LIMIT"
    assert payload2["price"] == 300.5


def test_invalid_schedule_entries_are_filtered(strategy_manager):
    strat = Strategy(
        name="broken_strategy",
        strategy_type="internal",
        enabled=True,
        schedule=[
            {"time": "invalid", "symbol": "AAPL", "action": "BUY", "qty": 1},
            {"time": "10:15", "symbol": None, "action": "BUY", "qty": 1},
            {"time": "11:00", "symbol": "TSLA", "action": "SELL", "qty": 1},
        ],
    )
    strategy_manager.strategies[strat.name] = strat

    strategy_manager.schedule_strategy_orders()

    assert len(strategy_manager.scheduler.jobs) == 1
    job = strategy_manager.scheduler.jobs[0]
    payload = job["args"][0]
    assert payload["symbol"] == "TSLA"
    assert payload["action"] == "SELL"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
