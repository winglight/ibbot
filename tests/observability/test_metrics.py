from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.observability.metrics import (
    build_metrics_router,
    record_ib_reconnect,
    record_redis_publish_failure,
    reset_metrics,
    set_subscription_gauge,
)


def test_metrics_router_exports_counters() -> None:
    reset_metrics()
    app = FastAPI()
    app.include_router(build_metrics_router())
    client = TestClient(app)

    record_ib_reconnect(service="orders")
    record_redis_publish_failure(service="orders")
    set_subscription_gauge(service="market-data", channel="quotes", count=3)

    response = client.get("/metrics")
    assert response.status_code == 200
    content = response.text
    assert "ibbot_ib_client_reconnects_total" in content
    assert "orders" in content
    assert "ibbot_active_subscriptions" in content
    assert "market-data" in content
