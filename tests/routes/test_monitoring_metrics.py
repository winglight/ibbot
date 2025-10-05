import datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.routes import monitoring as monitoring_module
from src.ibbot.redis import RedisConnectionError


@pytest.fixture()
def metrics_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    app = FastAPI()
    app.include_router(monitoring_module.router)
    base_time = datetime.datetime.now(datetime.timezone.utc).replace(second=0, microsecond=0)
    samples: list[dict[str, Any]] = [
        {
            "time": (base_time - datetime.timedelta(minutes=5)).isoformat(),
            "service": "queue-monitor",
            "metric": "queue-length",
            "queue_key": "queue:ib:commands",
            "value": 3.0,
        },
        {
            "time": (base_time - datetime.timedelta(minutes=5)).isoformat(),
            "service": "queue-monitor",
            "metric": "queue-length",
            "queue_key": "queue:ib:commands",
            "value": 2.0,
        },
        {
            "time": (base_time - datetime.timedelta(minutes=3)).isoformat(),
            "service": "market-data",
            "metric": "throughput",
            "stream_key": "stream:market",
            "group": "market-group",
            "value": 5.0,
        },
    ]

    async def fake_load_service_metrics(
        start: datetime.datetime,
        end: datetime.datetime | None = None,
        *,
        service: str | None = None,
        metric: str | None = None,
        redis: Any | None = None,
    ) -> list[dict[str, Any]]:
        _ = (start, end, redis)
        filtered: list[dict[str, Any]] = []
        for entry in samples:
            if service and entry.get("service") != service:
                continue
            if metric and entry.get("metric") != metric:
                continue
            filtered.append(dict(entry))
        return filtered

    monkeypatch.setattr(monitoring_module, "load_service_metrics", fake_load_service_metrics)
    return app


def test_metrics_route_returns_series(metrics_app: FastAPI) -> None:
    with TestClient(metrics_app) as client:
        response = client.get("/api/monitoring/metrics/write-rate", params={"window": "1h"})
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
    with TestClient(metrics_app) as client:
        response = client.get("/api/monitoring/influx/write-rate", params={"window": "1h"})
        secondary = client.get("/monitoring/influx/write-rate", params={"window": "1h"})
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
    with TestClient(metrics_app) as client:
        response = client.get(
            "/api/monitoring/metrics/write-rate",
            params={"window": "1h", "service": "market-data"},
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert len(payload["series"]) == 1
    entry = payload["series"][0]
    assert entry["service"] == "market-data"
    assert entry["metric"] == "throughput"


def test_metrics_route_rejects_invalid_aggregate(metrics_app: FastAPI) -> None:
    with TestClient(metrics_app) as client:
        response = client.get(
            "/api/monitoring/metrics/write-rate",
            params={"window": "1h", "aggregate": "median"},
        )
    assert response.status_code == 400


def test_metrics_route_rejects_invalid_window(metrics_app: FastAPI) -> None:
    with TestClient(metrics_app) as client:
        response = client.get(
            "/api/monitoring/metrics/write-rate",
            params={"window": "abc"},
        )
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


def test_queue_metrics_returns_timeout_quickly(
    metrics_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    class SlowRedis:
        async def hgetall(self, *_: object, **__: object) -> dict[str, Any]:
            await asyncio.sleep(1)
            return {}

    async def fake_get_redis_client(*_: object, **__: object) -> SlowRedis:
        return SlowRedis()

    monkeypatch.setattr(monitoring_module, "get_redis_client", fake_get_redis_client)
    monkeypatch.setattr(monitoring_module, "MONITORING_REDIS_COMMAND_TIMEOUT", 0.05)

    start = time.perf_counter()
    with TestClient(metrics_app) as client:
        response = client.get("/api/monitoring/queues")
    duration = time.perf_counter() - start

    assert response.status_code == 503
    assert response.json() == {
        "detail": {"error": "redis_unavailable", "message": "Redis 服务不可用"}
    }
    assert duration < 0.5
