from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from src.utils import audit as audit_module


class DummyCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.closed = False

    def execute(self, sql: str, params: Any) -> None:
        self.executed.append((sql, params))

    def close(self) -> None:
        self.closed = True


class DummyConnection:
    def __init__(self, should_fail: bool = False) -> None:
        self.cursor_obj = DummyCursor()
        self.should_fail = should_fail
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self) -> DummyCursor:
        return self.cursor_obj

    def commit(self) -> None:
        if self.should_fail:
            raise RuntimeError("commit failed")
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


class FailingCursor(DummyCursor):
    def execute(self, sql: str, params: Any) -> None:  # noqa: D401 - override
        raise RuntimeError("boom")


class FailingConnection(DummyConnection):
    def __init__(self) -> None:
        super().__init__(should_fail=True)
        self.cursor_obj = FailingCursor()


class FakeLogger:
    def __init__(self) -> None:
        self.warning_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.debug_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.info_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.bind_calls: list[dict[str, Any]] = []

    def warning(self, message: str, *args: Any, **_kwargs: Any) -> None:
        self.warning_calls.append((message, args))

    def debug(self, message: str, *args: Any, **_kwargs: Any) -> None:
        self.debug_calls.append((message, args))

    def info(self, message: str, *args: Any, **_kwargs: Any) -> None:
        self.info_calls.append((message, args))

    def bind(self, **kwargs: Any) -> "FakeLogger":
        self.bind_calls.append(dict(kwargs))
        return self


def _fake_request() -> Any:
    scope = SimpleNamespace(path="/orders")
    state = SimpleNamespace(username="alice", user_role="operator", is_read_only=False)
    return SimpleNamespace(method="post", url=scope, state=state)


@pytest.mark.asyncio
async def test_log_audit_event_persists_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = DummyConnection()

    @contextmanager
    def fake_connect() -> Any:
        yield connection

    async def run_in_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    monkeypatch.setattr(audit_module, "connect", fake_connect)
    monkeypatch.setattr(audit_module, "run_in_thread", run_in_thread)

    request = _fake_request()
    await audit_module.log_audit_event(
        request,
        action="orders:place",
        success=True,
        detail="order placed",
        extra={"quantity": 10},
    )

    assert connection.committed is True
    assert connection.cursor_obj.executed, "expected SQL execution"
    sql, params = connection.cursor_obj.executed[0]
    assert "INSERT INTO audit_logs" in sql
    assert params["action"] == "orders:place"
    assert json.loads(params["extra"])["quantity"] == 10


@pytest.mark.asyncio
async def test_log_audit_event_handles_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_logger = FakeLogger()

    def raise_config_error() -> Any:
        raise audit_module.MariaDBConfigurationError("missing db url")

    async def run_in_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    monkeypatch.setattr(audit_module, "connect", raise_config_error)
    monkeypatch.setattr(audit_module, "logger", fake_logger)
    monkeypatch.setattr(audit_module, "run_in_thread", run_in_thread)

    request = _fake_request()
    await audit_module.log_audit_event(request, action="orders:cancel", success=False)

    assert fake_logger.warning_calls, "expected warning to be emitted"
    assert fake_logger.info_calls, "expected fallback log entry"


@pytest.mark.asyncio
async def test_log_audit_event_logs_generic_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = FailingConnection()
    fake_logger = FakeLogger()

    @contextmanager
    def fake_connect() -> Any:
        yield connection

    async def run_in_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    monkeypatch.setattr(audit_module, "connect", fake_connect)
    monkeypatch.setattr(audit_module, "logger", fake_logger)
    monkeypatch.setattr(audit_module, "run_in_thread", run_in_thread)

    request = _fake_request()
    await audit_module.log_audit_event(request, action="orders:update", success=True)

    assert fake_logger.debug_calls, "expected debug log for failure"
    assert fake_logger.info_calls, "expected fallback audit log"
