import asyncio

import pytest
import time
from fastapi import FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient

import core
from src.dom_cache import DomCacheManager
from src.dom_optimizer import DOMOptimizationOrchestrator
from src.dom_service import get_dom_service
from src.dom_stats import DomStatsAggregator
from src.routes.auth import create_access_token
from src.routes.dom import router as dom_router


@pytest.fixture
def client(monkeypatch, tmp_path):
    app = FastAPI()
    app.include_router(dom_router)
    token, _ = create_access_token({"sub": "tester"})
    headers = {"Authorization": f"Bearer {token}"}

    # Patch connection and depth subscriber
    monkeypatch.setattr(core.connection_manager, "is_connected", lambda: True)
    callbacks = {}

    def fake_start(symbol, cb, num_rows=5):
        callbacks[symbol] = cb
        return True

    def fake_stop(symbol):
        callbacks.pop(symbol, None)
        return True

    monkeypatch.setattr(core.depth_subscriber, "start_depth_stream", fake_start)
    monkeypatch.setattr(core.depth_subscriber, "stop_depth_stream", fake_stop)

    dom_service = get_dom_service()
    dom_service.update_config(50, 10)
    dom_service._alerts.clear()
    dom_service._last_depth_by_symbol.clear()
    dom_service._last_metrics_by_symbol.clear()
    if hasattr(dom_service, "_stats") and dom_service._stats:
        try:
            dom_service._stats.shutdown()
        except Exception:
            pass
    dom_service._cache = DomCacheManager(base_dir=str(tmp_path))
    dom_service._stats = DomStatsAggregator(dom_service._cache, base_dir=str(tmp_path))

    class DummyDB:
        def __init__(self):
            self.storage = {}

        def save_dom_config(self, payload):
            self.storage["dom_config"] = payload

        def load_dom_config(self):
            return {}

        def save_config(self, key, value, description=None):
            self.storage[key] = value

        def get_config(self, key, default=None):
            return self.storage.get(key, default)

    dummy_db = DummyDB()
    dom_service.db_manager = dummy_db

    optimizer = DOMOptimizationOrchestrator(
        service=dom_service, cache=dom_service._cache, db_manager=dummy_db
    )
    optimizer.reset_state()
    monkeypatch.setattr("src.dom_optimizer._optimizer_instance", optimizer)

    try:
        yield TestClient(app), headers, callbacks, dom_service
    finally:
        try:
            dom_service._stats.shutdown()
        except Exception:
            pass
        optimizer.shutdown()


def _feed(callback):
    callback({"bids": [(100, 10)], "asks": [(101, 100)], "ts_ns": 1})
    callback({"bids": [(101, 20)], "asks": [(102, 100)], "ts_ns": 2})


def test_dom_api_flow(client, monkeypatch):
    test_client, headers, callbacks, dom_service = client

    # start stream
    r = test_client.post("/api/dom/start", json={"symbol": "TEST"}, headers=headers)
    assert r.status_code == 200 and r.json()["success"]
    cb = callbacks["TEST"]

    # feed two snapshots to trigger metrics and alert
    _feed(cb)

    # metrics endpoint
    r = test_client.get("/api/dom/data", params={"symbol": "TEST"}, headers=headers)
    metrics = r.json()["metrics"]
    assert metrics["obi5"] == pytest.approx(20 / 120)
    assert "timestamp" in metrics

    history_resp = test_client.get("/api/dom/history/TEST", headers=headers)
    assert history_resp.status_code == 200
    history = history_resp.json()
    assert history["success"] is True
    assert len(history.get("metrics", [])) >= 1
    assert history["total_metrics"] >= len(history["metrics"])

    latest_metric = history["metrics"][-1]
    latest_ts = latest_metric.get("timestamp")
    assert latest_ts is not None

    trimmed = test_client.get(
        "/api/dom/history/TEST",
        params={"limit": 1, "include_snapshots": False, "start": latest_ts, "end": latest_ts},
        headers=headers,
    )
    assert trimmed.status_code == 200
    trimmed_history = trimmed.json()
    assert trimmed_history["metrics"]
    assert trimmed_history["metrics"][0]["timestamp"] == latest_ts
    assert trimmed_history["snapshots"] == []
    assert trimmed_history["total_metrics"] == 1

    stats_resp = test_client.get("/api/dom/stats/TEST", headers=headers)
    assert stats_resp.status_code == 200
    stats_payload = stats_resp.json()
    assert stats_payload["success"] is True
    assert "stats" in stats_payload and "chart_metrics" in stats_payload
    assert isinstance(stats_payload["chart_metrics"], list)
    assert stats_payload["stats"].get("total") >= 0

    lookback_resp = test_client.get(
        "/api/dom/stats/TEST",
        params={"lookback_seconds": 1200, "max_points": 50},
        headers=headers,
    )
    assert lookback_resp.status_code == 200
    lookback_payload = lookback_resp.json()
    assert lookback_payload["success"] is True
    assert lookback_payload["metadata"]["version"] == DomStatsAggregator.VERSION

    # alerts list and ack
    r = test_client.get("/api/alerts", headers=headers)
    alerts = r.json()["alerts"]
    assert len(alerts) >= 1
    assert any(alert.get("event", {}).get("code") for alert in alerts)
    for alert in alerts:
        r = test_client.post(f"/api/alerts/{alert['id']}/ack", headers=headers)
        assert r.json()["success"]
    r = test_client.get("/api/alerts", headers=headers)
    assert r.json().get("alerts") == []

    # websocket
    original_sleep = asyncio.sleep

    async def instant_sleep(_):
        await original_sleep(0)

    monkeypatch.setattr("src.routes.dom.asyncio.sleep", instant_sleep)
    with test_client.websocket_connect("/ws/dom/TEST") as ws:
        data = ws.receive_json()
        assert "metrics" in data and "events" in data

    # stop stream
    r = test_client.post("/api/dom/stop", json={"symbol": "TEST"}, headers=headers)
    assert r.status_code == 200 and r.json()["success"]


def test_dom_ws_reports_unavailable_and_stale_states(monkeypatch):
    from src.routes.dom import dom_ws

    class FakeWebSocket:
        def __init__(self, max_messages: int):
            self.max_messages = max_messages
            self.messages = []

        async def accept(self):
            return None

        async def send_json(self, data):
            self.messages.append(data)
            if len(self.messages) >= self.max_messages:
                raise WebSocketDisconnect(code=1000)

    original_sleep = asyncio.sleep

    async def instant_sleep(_):
        await original_sleep(0)

    monkeypatch.setattr("src.routes.dom.asyncio.sleep", instant_sleep)

    def make_metrics_provider(payloads):
        iterator = iter(payloads)

        def _get_metrics(symbol):
            try:
                return next(iterator)
            except StopIteration:
                return {}

        return _get_metrics

    monkeypatch.setattr("src.routes.dom.dom_service.get_events", lambda: [])

    # Case 1: no metrics available should yield an explicit error
    monkeypatch.setattr(
        "src.routes.dom.dom_service.get_metrics",
        make_metrics_provider([{}]),
    )
    ws_no_metrics = FakeWebSocket(max_messages=1)
    asyncio.run(dom_ws(ws_no_metrics, "UNKNOWN"))
    assert ws_no_metrics.messages == [
        {
            "symbol": "UNKNOWN",
            "metrics": None,
            "events": [],
            "error": "DOM metrics unavailable",
        }
    ]

    # Case 2: repeated metrics should raise a stale-data error on subsequent iteration
    monkeypatch.setattr(
        "src.routes.dom.dom_service.get_metrics",
        make_metrics_provider(
            [
                {"timestamp": 1.0, "obi5": 0.5},
                {"timestamp": 1.0, "obi5": 0.5},
            ]
        ),
    )
    ws_stale = FakeWebSocket(max_messages=2)
    asyncio.run(dom_ws(ws_stale, "TEST"))
    assert ws_stale.messages[0]["metrics"] == {"timestamp": 1.0, "obi5": 0.5}
    assert "error" not in ws_stale.messages[0]
    assert ws_stale.messages[1]["metrics"] is None
    assert ws_stale.messages[1]["error"] == "DOM metrics stale: no new data received"


def test_dom_optimization_endpoints(client):
    test_client, headers, _, dom_service = client

    base_ts = time.time() * 1000.0
    metrics = [
        {
            "timestamp": base_ts + 1,
            "obi5": 0.72,
            "ofi": 6.0,
            "signal_type": "long",
            "outcome": 1,
        },
        {
            "timestamp": base_ts + 2,
            "obi5": 0.68,
            "ofi": 5.5,
            "signal_type": "long",
            "outcome": 0,
        },
    ]
    for item in metrics:
        dom_service._cache.append("TEST", {"timestamp": item["timestamp"]}, item)

    resp = test_client.post(
        "/api/dom/optimize",
        json={
            "symbol": "TEST",
            "min_signals": 1,
            "feature_thresholds": {"stacking": {"intensity": 0.45}},
        },
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["evaluated_records"] >= 1
    assert data["duration_ms"] > 0
    assert data["started_at"]
    assert data["completed_at"]
    assert data["feature_thresholds"]["stacking"]["intensity"] == pytest.approx(0.45)

    status_resp = test_client.get("/api/dom/automation/status", headers=headers)
    assert status_resp.status_code == 200
    status_data = status_resp.json()
    assert status_data["success"] is True
    assert status_data["status"] in {"succeeded", "running", "failed"}
    assert "plan" in status_data
    assert status_data["last_started_at"]
    assert status_data["last_run_at"]

    schedule_resp = test_client.post(
        "/api/dom/automation/schedule",
        json={
            "enabled": True,
            "interval_minutes": 30,
            "symbol": "TEST",
            "lookback_minutes": 15,
            "apply_updates": False,
            "feature_thresholds": {"cancel_rate": {"rate": 1.5}},
        },
        headers=headers,
    )
    assert schedule_resp.status_code == 200
    schedule_data = schedule_resp.json()
    assert schedule_data["success"] is True
    assert schedule_data["schedule"]["enabled"] is True
    assert schedule_data["schedule"]["interval_minutes"] == 30
    assert schedule_data["plan"]["symbol"] == "TEST"
    assert schedule_data["plan"]["lookback_minutes"] == 15
    assert schedule_data["plan"]["apply_updates"] is False
    assert schedule_data["plan"]["feature_thresholds"]["cancel_rate"]["rate"] == pytest.approx(1.5)


def test_dom_automation_requires_write_access(client):
    test_client, _, _, _ = client
    token, _ = create_access_token(
        {"sub": "readonly", "role": "visitor", "read_only": True}
    )
    ro_headers = {"Authorization": f"Bearer {token}"}

    resp_optimize = test_client.post(
        "/api/dom/optimize",
        json={"symbol": "TEST"},
        headers=ro_headers,
    )
    assert resp_optimize.status_code == 403

    resp_schedule = test_client.post(
        "/api/dom/automation/schedule",
        json={
            "enabled": True,
            "interval_minutes": 30,
            "symbol": "TEST",
        },
        headers=ro_headers,
    )
    assert resp_schedule.status_code == 403
