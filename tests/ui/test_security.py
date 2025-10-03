from __future__ import annotations

from typing import Any
import json
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.routes import auth as auth_module
from src.routes import orders as orders_module


class QueueRecorder:
    def __init__(self) -> None:
        self.commands: list[dict[str, Any]] = []
        self.should_fail = False

    async def enqueue(self, payload: dict[str, Any], *, queue_key: str) -> None:
        if self.should_fail:
            raise RuntimeError("redis offline")
        record = dict(payload)
        record["queue_key"] = queue_key
        self.commands.append(record)


@pytest.fixture()
def secured_orders_app(monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, QueueRecorder]:
    app = FastAPI()
    app.include_router(orders_module.router)
    orders_module.set_websocket_broadcast_override(None)

    recorder = QueueRecorder()

    async def operator(request: Request) -> str:
        request.state.is_read_only = False
        request.state.user_role = "operator"
        request.state.username = "operator"
        return "operator"

    app.dependency_overrides[orders_module.get_current_user] = operator
    app.dependency_overrides[orders_module.get_orders_service_client] = lambda: None

    async def fake_enqueue(payload: dict[str, Any], *, queue_key: str) -> None:
        await recorder.enqueue(payload, queue_key=queue_key)

    async def fake_queue_key() -> str:
        return "queue:test"

    class FakeUUID:
        hex = "cmd-sec"

    monkeypatch.setattr(orders_module, "enqueue_ib_command", fake_enqueue)
    monkeypatch.setattr(orders_module, "_resolve_command_queue_key", fake_queue_key)
    monkeypatch.setattr(orders_module.uuid, "uuid4", lambda: FakeUUID())
    return app, recorder


def test_place_order_audit_logged_for_operator(
    secured_orders_app: tuple[FastAPI, QueueRecorder],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, recorder = secured_orders_app
    success_events: list[dict[str, Any]] = []
    failure_events: list[dict[str, Any]] = []

    async def record_success(
        request: Request,
        *,
        action: str | None = None,
        detail: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        success_events.append({"action": action, "detail": detail, "extra": extra, "path": request.url.path})

    async def record_failure(
        request: Request,
        *,
        action: str | None = None,
        detail: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        failure_events.append({"action": action, "detail": detail, "extra": extra, "path": request.url.path})

    monkeypatch.setattr(orders_module, "audit_log_success", record_success)
    monkeypatch.setattr(orders_module, "audit_log_failure", record_failure)

    with TestClient(app) as client:
        response = client.post(
            "/api/place_order",
            json={
                "symbol": "ES",
                "quantity": 1,
                "action": "BUY",
                "order_type": "market",
                "exchange": "CME",
                "sec_type": "FUT",
            },
        )

    assert response.status_code == 200
    assert success_events, "expected audit success to be recorded"
    event = success_events[0]
    assert event["detail"].startswith("order command")
    assert event["extra"]["symbol"] == "ES"
    assert failure_events == []
    assert len(recorder.commands) == 1


def test_forbidden_role_records_audit_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastAPI()
    app.include_router(orders_module.router)
    orders_module.set_websocket_broadcast_override(None)

    recorder = QueueRecorder()

    async def auditor(request: Request) -> str:
        request.state.is_read_only = False
        request.state.user_role = "auditor"
        request.state.username = "auditor"
        return "auditor"

    captured: list[dict[str, Any]] = []

    async def capture_event(
        request: Request,
        *,
        action: str,
        success: bool,
        detail: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        captured.append({"action": action, "success": success, "detail": detail, "path": request.url.path})

    monkeypatch.setattr(auth_module, "log_audit_event", capture_event)
    app.dependency_overrides[orders_module.get_current_user] = auditor
    app.dependency_overrides[orders_module.get_orders_service_client] = lambda: None

    async def fake_enqueue(payload: dict[str, Any], *, queue_key: str) -> None:
        await recorder.enqueue(payload, queue_key=queue_key)

    async def fake_queue_key() -> str:
        return "queue:test"

    monkeypatch.setattr(orders_module, "enqueue_ib_command", fake_enqueue)
    monkeypatch.setattr(orders_module, "_resolve_command_queue_key", fake_queue_key)

    with TestClient(app) as client:
        response = client.post(
            "/api/place_order",
            json={
                "symbol": "ES",
                "quantity": 1,
                "action": "BUY",
                "order_type": "market",
                "exchange": "CME",
                "sec_type": "FUT",
            },
        )

    assert response.status_code == 403
    payload = response.json()
    assert payload["detail"]["error"] == "RBAC_FORBIDDEN"
    assert captured, "expected audit failure to be recorded"
    record = captured[0]
    assert record["success"] is False
    assert record["action"] == "orders.place"
    assert recorder.commands == []


def test_frontend_api_call_handles_rbac_forbidden(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    script_path = (repo_root / 'static/js/app.js').as_posix()
    node_script = """
const fs = require('fs');
const vm = require('vm');
let code = fs.readFileSync(__SCRIPT_PATH__, 'utf8');
code = code.replace('const app = new TradingApp();', 'globalThis.__TradingApp__ = TradingApp;');
code = code.replace('window.app = app;', '');

const context = {
  console,
  setTimeout,
  clearTimeout,
  window: {},
  document: { body: { classList: { toggle: () => {} } }, getElementById: () => null },
  localStorage: (() => {
    const store = new Map();
    return {
      getItem(key) { return store.has(key) ? store.get(key) : null; },
      setItem(key, value) { store.set(key, String(value)); },
      removeItem(key) { store.delete(key); },
    };
  })(),
  appModules: {
    monitoring: {
      constants: {},
      utils: {
        normalizeIntervalKey: () => '1m',
        normalizeDurationKey: () => '1d',
        getMonitorDurationMinutes: () => 1,
        getMonitorDurationOptions: () => ({}),
        refreshMonitorDurationSelect: () => {},
        getTrendLevelStyle: () => ({}),
        getTrendSuggestionLabel: () => 'ok',
      },
      manager: { MonitoringManager: function() { return {}; } },
      dashboard: { MonitoringDashboard: function() { return {}; } },
    },
    utils: {
      TrendSignalLabelPlugin: { afterDatasetsDraw: () => {} },
      slugifyStrategyName: (name) => name,
      formatNumber: (n) => String(n),
      escapeHtml: (v) => v,
      formatDuration: (v) => v,
      isSameDay: () => true,
      formatOrderIdDisplay: () => 'cmd',
    },
    strategies: {
      manager: { StrategiesManager: function() { return {}; } },
      details: { StrategyDetailsController: function() { return {}; } },
    },
    ui: { controller: { UIController: function() { return { showAlert: () => {} }; } } },
    notifications: { manager: { NotificationsManager: function() { return {}; } } },
    serviceRestart: {},
    configCenter: {},
    orders: { manager: { OrderManager: function() { return {}; } } },
  },
};
context.globalThis = context;
context.window = context;
vm.createContext(context);
vm.runInContext(code, context);

const TradingApp = context.__TradingApp__;
const apiCall = TradingApp.prototype.apiCall;

const alerts = [];
const fakeApp = {
  isReadOnly: false,
  apiCache: {},
  lastReadOnlyWarningAt: 0,
  loginManager: { toggleAuthButtons: () => {}, showLoginModal: () => {} },
  showAlert: (type, message) => { alerts.push({ type, message }); },
  fetchWithTimeout: async () => ({
    status: 403,
    headers: { get: () => null },
    text: async () => JSON.stringify({ detail: { error: 'RBAC_FORBIDDEN', message: 'Forbidden', required_roles: ['admin'] } }),
  }),
};

async function run() {
  try {
    await apiCall.call(fakeApp, '/orders', { method: 'POST' });
    console.log(JSON.stringify({ ok: true, alerts }));
  } catch (err) {
    console.log(JSON.stringify({
      ok: false,
      message: err.message,
      code: err.code,
      requiredRoles: err.requiredRoles,
      alerts,
    }));
  }
}

run();
"""
    script_literal = json.dumps(script_path)
    completed = subprocess.run(
        ["node", "-e", node_script.replace('__SCRIPT_PATH__', script_literal)],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout.strip() or "{}")
    assert payload["ok"] is False
    assert payload["code"] == "RBAC_FORBIDDEN"
    assert payload["requiredRoles"] == ["admin"]
    assert payload["alerts"] and payload["alerts"][0]["message"].startswith("You do not have permission")
