import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import core
from src.dom_cache import DomCacheManager
from src.dom_service import get_dom_service
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
    dom_service._cache = DomCacheManager(base_dir=str(tmp_path))

    return TestClient(app), headers, callbacks, dom_service


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

    # alerts list and ack
    r = test_client.get("/api/alerts", headers=headers)
    alerts = r.json()["alerts"]
    assert len(alerts) == 1
    alert_id = alerts[0]["id"]
    r = test_client.post(f"/api/alerts/{alert_id}/ack", headers=headers)
    assert r.json()["success"]
    r = test_client.get("/api/alerts", headers=headers)
    assert r.json()["alerts"] == []

    # websocket
    async def instant_sleep(_):
        pass

    monkeypatch.setattr("src.routes.dom.asyncio.sleep", instant_sleep)
    with test_client.websocket_connect("/ws/dom/TEST") as ws:
        data = ws.receive_json()
        assert "metrics" in data and "events" in data

    # stop stream
    r = test_client.post("/api/dom/stop", json={"symbol": "TEST"}, headers=headers)
    assert r.status_code == 200 and r.json()["success"]
