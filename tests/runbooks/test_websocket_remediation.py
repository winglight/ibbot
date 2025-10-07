import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.data_layer import DataSourceError
from src.runbooks import websocket_remediation as runbook


class DummyEventBus:
    def __init__(self, loop: asyncio.AbstractEventLoop, event: object | None) -> None:
        self._loop = loop
        self._event = event
        self.unsubscribed: list[str] = []

    def subscribe(self, channel: str, handler):  # type: ignore[override]
        token = f"{channel}-token"
        if self._event is not None:
            self._loop.call_soon(handler, self._event)
        return token

    def unsubscribe(self, token: str) -> None:
        self.unsubscribed.append(token)


class DummyManager:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        snapshot: dict | None,
        event: object | None,
        snapshot_error: Exception | None = None,
        subscribe_error: Exception | None = None,
    ) -> None:
        self.event_bus = DummyEventBus(loop, event)
        self._snapshot = snapshot
        self._snapshot_error = snapshot_error
        self._subscribe_error = subscribe_error
        self.unsubscribe_calls: list[str] = []

    async def snapshot(self, request):  # type: ignore[override]
        if self._snapshot_error is not None:
            raise self._snapshot_error
        return self._snapshot

    async def subscribe(self, request):  # type: ignore[override]
        if self._subscribe_error is not None:
            raise self._subscribe_error
        return "subscription-token"

    async def unsubscribe(self, token: str) -> None:
        self.unsubscribe_calls.append(token)


class DummyDomService:
    def __init__(self, history: dict[str, object]) -> None:
        self._history = history

    def get_history(self, symbol: str, *, limit: int = 1, include_snapshots: bool = False):  # type: ignore[override]
        return self._history


@pytest.mark.asyncio
async def test_realtime_price_step_success(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = asyncio.get_running_loop()
    event = SimpleNamespace(symbol="ES", ts_ns=2_000_000_000, source="feed")
    manager = DummyManager(loop, snapshot={"ts_ns": 1_000_000_000, "last": 4200.0}, event=event)

    monkeypatch.setenv("RUNBOOK_MARKET_WAIT_SECONDS", "0.01")
    monkeypatch.setattr(runbook, "get_data_source_manager", lambda: manager)

    result = await runbook._realtime_price_step()

    assert result.status == "success"
    assert result.details["event"]["source"] == "feed"
    assert result.details["snapshot"]["last"] == 4200.0


@pytest.mark.asyncio
async def test_realtime_price_step_warning_without_event(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = asyncio.get_running_loop()
    manager = DummyManager(loop, snapshot={"ts_ns": 1_000_000_000, "last": 4200.0}, event=None)

    monkeypatch.setenv("RUNBOOK_MARKET_WAIT_SECONDS", "0.01")
    monkeypatch.setattr(runbook, "get_data_source_manager", lambda: manager)

    result = await runbook._realtime_price_step()

    assert result.status == "warning"
    assert "未收到推送" in result.details["message"]


@pytest.mark.asyncio
async def test_realtime_price_step_handles_snapshot_error(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = asyncio.get_running_loop()
    manager = DummyManager(loop, snapshot=None, event=None, snapshot_error=DataSourceError("boom"))

    monkeypatch.setattr(runbook, "get_data_source_manager", lambda: manager)

    result = await runbook._realtime_price_step()

    assert result.status == "error"
    assert "读取快照失败" in result.details["message"]


@pytest.mark.asyncio
async def test_dom_depth_step_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    loop = asyncio.get_running_loop()
    event = SimpleNamespace(symbol="ES", ts_ns=3_000_000_000, source="feed")
    snapshot = {"ts_ns": 2_000_000_000, "bids": [{}], "asks": [{}]}
    manager = DummyManager(loop, snapshot=snapshot, event=event)

    summary = SimpleNamespace(files=[], total_rows=10, earliest_ns=1_000_000_000, latest_ns=3_000_000_000)
    history = {"total_snapshots": 4, "total_metrics": 2, "metrics": [{"timestamp": 1_700_000_000_000}]}

    monkeypatch.setenv("RUNBOOK_DOM_WAIT_SECONDS", "0.01")
    monkeypatch.setattr(runbook, "get_data_source_manager", lambda: manager)
    monkeypatch.setattr(runbook, "get_market_data_writer", lambda: SimpleNamespace(base_dir=tmp_path))
    monkeypatch.setattr(runbook, "summarise_dom_directory", lambda base_dir, symbol: summary)
    monkeypatch.setattr(runbook, "get_dom_service", lambda: DummyDomService(history))

    result = await runbook._dom_depth_step()

    assert result.status == "success"
    assert result.details["storage"]["total_rows"] == 10
    assert result.details["cache"]["total_snapshots"] == 4


@pytest.mark.asyncio
async def test_dom_depth_step_error_without_snapshot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    loop = asyncio.get_running_loop()
    manager = DummyManager(loop, snapshot=None, event=None)

    summary = SimpleNamespace(files=[], total_rows=0, earliest_ns=None, latest_ns=None)
    history = {"total_snapshots": 0, "total_metrics": 0, "metrics": []}

    monkeypatch.setenv("RUNBOOK_DOM_WAIT_SECONDS", "0.01")
    monkeypatch.setattr(runbook, "get_data_source_manager", lambda: manager)
    monkeypatch.setattr(runbook, "get_market_data_writer", lambda: SimpleNamespace(base_dir=tmp_path))
    monkeypatch.setattr(runbook, "summarise_dom_directory", lambda base_dir, symbol: summary)
    monkeypatch.setattr(runbook, "get_dom_service", lambda: DummyDomService(history))

    result = await runbook._dom_depth_step()

    assert result.status == "error"
    assert "缺少盘口快照" in result.details["message"]
