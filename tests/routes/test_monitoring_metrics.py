import datetime
import sqlite3
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.db import set_engine
from src.routes import monitoring as monitoring_module
from src.ibbot.redis import RedisConnectionError


@pytest.fixture()
def metrics_app() -> FastAPI:
    app = FastAPI()
    app.include_router(monitoring_module.router)
    yield app
    set_engine(None)


def _prepare_metrics_table(connection: sqlite3.Connection) -> None:
    cursor = connection.cursor()
    cursor.execute(
        """
        CREATE TABLE service_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_at DATETIME NOT NULL,
            service TEXT,
            metric TEXT,
            queue_key TEXT,
            stream_key TEXT,
            group_name TEXT,
            value REAL
        )
        """
    )

    base_time = datetime.datetime.utcnow().replace(second=0, microsecond=0)
    rows: list[tuple[Any, ...]] = [
        (
            base_time - datetime.timedelta(minutes=5),
            "queue-monitor",
            "queue-length",
            "queue:ib:commands",
            None,
            None,
            3.0,
        ),
        (
            base_time - datetime.timedelta(minutes=5) + datetime.timedelta(seconds=30),
            "queue-monitor",
            "queue-length",
            "queue:ib:commands",
            None,
            None,
            2.0,
        ),
        (
            base_time - datetime.timedelta(minutes=3),
            "market-data",
            "throughput",
            None,
            "stream:market",
            "market-group",
            5.0,
        ),
    ]
    cursor.executemany(
        """
        INSERT INTO service_metrics (
            recorded_at,
            service,
            metric,
            queue_key,
            stream_key,
            group_name,
            value
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    connection.commit()
    cursor.close()


def test_metrics_route_returns_series(metrics_app: FastAPI) -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    set_engine(connection)
    _prepare_metrics_table(connection)

    with TestClient(metrics_app) as client:
        response = client.get("/api/monitoring/metrics/write-rate", params={"window": "1h"})

    connection.close()
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["query"]["window"] == "1h"
    assert len(payload["series"]) == 2

    first_entry = payload["series"][0]
    assert first_entry["service"] == "queue-monitor"
    assert first_entry["metric"] == "queue-length"
    assert pytest.approx(first_entry["value"], rel=1e-6) == 5.0

    summary = payload["summary"]
    assert summary["queue-length"]["points"] == 1
    assert pytest.approx(summary["queue-length"]["average"], rel=1e-6) == 5.0


def test_metrics_legacy_influx_route_alias(metrics_app: FastAPI) -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    set_engine(connection)
    _prepare_metrics_table(connection)

    with TestClient(metrics_app) as client:
        response = client.get("/api/monitoring/influx/write-rate", params={"window": "1h"})
        secondary = client.get("/monitoring/influx/write-rate", params={"window": "1h"})

    connection.close()
    assert response.status_code == 200
    assert secondary.status_code == 200

    payload = response.json()
    legacy = secondary.json()
    assert payload["success"] is True
    assert payload.get("legacy") is True
    assert payload["query"]["window"] == "1h"
    assert legacy.get("legacy") is True
    assert legacy["query"]["window"] == "1h"
    assert payload["series"] == legacy["series"]


def test_metrics_route_filters_by_service(metrics_app: FastAPI) -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    set_engine(connection)
    _prepare_metrics_table(connection)

    with TestClient(metrics_app) as client:
        response = client.get(
            "/api/monitoring/metrics/write-rate",
            params={"window": "1h", "service": "market-data"},
        )

    connection.close()
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert len(payload["series"]) == 1
    entry = payload["series"][0]
    assert entry["service"] == "market-data"
    assert entry["metric"] == "throughput"


def test_metrics_route_rejects_invalid_aggregate(metrics_app: FastAPI) -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    set_engine(connection)
    _prepare_metrics_table(connection)

    with TestClient(metrics_app) as client:
        response = client.get(
            "/api/monitoring/metrics/write-rate",
            params={"window": "1h", "aggregate": "median"},
        )

    connection.close()
    assert response.status_code == 400


def test_metrics_route_rejects_invalid_window(metrics_app: FastAPI) -> None:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    set_engine(connection)
    _prepare_metrics_table(connection)

    with TestClient(metrics_app) as client:
        response = client.get(
            "/api/monitoring/metrics/write-rate",
            params={"window": "abc"},
        )

    connection.close()
    assert response.status_code == 400


def test_queue_metrics_returns_redis_unavailable(metrics_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_redis_client(*_: object, **__: object):
        raise RedisConnectionError("unreachable")

    monkeypatch.setattr(monitoring_module, "get_redis_client", fake_get_redis_client)

    with TestClient(metrics_app) as client:
        response = client.get("/api/monitoring/queues")

    assert response.status_code == 503
    assert response.json() == {
        "detail": {"error": "redis_unavailable", "message": "Redis 服务不可用"}
    }
