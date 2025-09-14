import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.routes.dom import router as dom_router
from src.routes.auth import create_access_token
import core
from src.dom_service import get_dom_service


@pytest.fixture
def client(monkeypatch):
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
    r = test_client.get("/api/dom/data", headers=headers)
    metrics = r.json()["metrics"]
    assert metrics["obi5"] == pytest.approx(20 / 120)

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
