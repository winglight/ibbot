import types

from src import strategy_manager as sm
from src.strategy_manager import StrategyManager, Strategy


class _FakeTime:
    def __init__(self, start: float = 1_000_000.0):
        self.current = float(start)

    def time(self) -> float:  # pragma: no cover - exercised via monkeypatch
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += float(seconds)


def _minimal_manager() -> StrategyManager:
    manager = StrategyManager.__new__(StrategyManager)
    manager.data_dir = None  # unused in tests
    manager.strategy_dir = None
    manager.external_clients = {}
    manager.db_manager = None
    manager.risk_manager = None
    manager.queue_manager = None
    manager.notification_manager = None
    manager.strategy_risk_manager = None
    manager.strategies = {}
    manager.settings = {}
    manager.internal_strategies_runtime = {}
    manager._loaded_internal_modules = {}
    manager._last_position_add = {}
    manager._auto_close_tasks = {}
    manager.data_push_state = {}
    manager._pending_data_push_requests = []
    manager._event_loop = None
    manager._strategies_cache = {"data": None, "expires_at": 0.0, "names": ()}
    manager._strategies_cache_lock = types.SimpleNamespace(__enter__=lambda self: None, __exit__=lambda self, *exc: None)
    manager._strategies_cache_ttl = 5.0
    manager.auth_token = None
    manager.scheduler = None
    manager._heartbeat_job_id = "test"
    manager._data_push_log_limit = 20
    return manager


def test_processed_count_keeps_data_push_active(monkeypatch):
    fake_time = _FakeTime()
    monkeypatch.setattr(sm.time, "time", fake_time.time)

    manager = _minimal_manager()
    name = "DOM Strategy"
    strategy = Strategy(name=name, strategy_type="internal")
    manager.strategies[name] = strategy
    manager.settings = {"data_push_restart_cooldown": 90}

    class DummyRuntime:
        def __init__(self):
            self.calls = 0
            self.active = True
            self.enabled = True

        def get_runtime_status(self):
            self.calls += 1
            return {
                "processed_count": self.calls,
                "last_data_timestamp": 0.0,
                "is_receiving_data": False,
            }

    runtime = DummyRuntime()
    manager.internal_strategies_runtime[name] = runtime

    manager.data_push_state[name] = {
        "enabled": True,
        "active": True,
        "subscription": "dom_trend",
        "symbol": "MNQ",
        "awaiting_data": True,
    }

    restart_called = {"flag": False}

    def record_restart(*_args, **_kwargs):
        restart_called["flag"] = True

    manager._request_data_push_activation = record_restart
    manager._record_data_push_event = lambda *args, **kwargs: None
    manager._set_data_push_reason = lambda *args, **kwargs: None

    status = manager.get_internal_strategy_status(name)

    assert status is not None
    assert status.get("is_receiving_data") is True
    assert manager.data_push_state[name]["awaiting_data"] is False
    assert restart_called["flag"] is False


def test_stall_detection_triggers_after_inactivity(monkeypatch):
    fake_time = _FakeTime()
    monkeypatch.setattr(sm.time, "time", fake_time.time)

    manager = _minimal_manager()
    name = "DOM Strategy"
    strategy = Strategy(name=name, strategy_type="internal")
    manager.strategies[name] = strategy
    manager.settings = {"data_push_restart_cooldown": 30}

    class DummyRuntime:
        def __init__(self):
            self.active = True
            self.enabled = True

        def get_runtime_status(self):
            return {
                "processed_count": 5,
                "last_data_timestamp": 0.0,
                "is_receiving_data": False,
            }

    runtime = DummyRuntime()
    manager.internal_strategies_runtime[name] = runtime

    manager.data_push_state[name] = {
        "enabled": True,
        "active": True,
        "subscription": "dom_trend",
        "symbol": "MES",
        "awaiting_data": False,
        "_last_processed_count": 5,
        "_last_processed_update": fake_time.time() - 40.0,
    }

    restart_called = {"flag": False}

    def record_restart(*_args, **_kwargs):
        restart_called["flag"] = True

    manager._request_data_push_activation = record_restart
    manager._record_data_push_event = lambda *args, **kwargs: None
    manager._set_data_push_reason = lambda *args, **kwargs: None

    status = manager.get_internal_strategy_status(name)

    assert status is not None
    assert restart_called["flag"] is True
