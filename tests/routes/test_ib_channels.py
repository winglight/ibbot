from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.routes import ib as ib_module


def test_ib_channels_endpoint_returns_expected_structure() -> None:
    app = FastAPI()
    app.include_router(ib_module.router)

    with TestClient(app) as client:
        response = client.get("/ib/channels")

    assert response.status_code == 200
    payload = response.json()

    assert payload.get("version") == 1
    services = payload.get("services")
    assert isinstance(services, dict)

    for service_key in ("account", "orders", "market_data"):
        assert service_key in services
        entry = services[service_key]
        assert "description" in entry
        redis_channels = entry.get("redis_channels")
        assert isinstance(redis_channels, list) and redis_channels

    account_endpoints = services["account"]["rest_endpoints"]
    assert {"subscribe", "unsubscribe", "summary", "positions"} <= account_endpoints.keys()

    orders_endpoints = services["orders"]["rest_endpoints"]
    assert {"place_order", "cancel_order", "open_orders"} <= orders_endpoints.keys()

    market_endpoints = services["market_data"]["rest_endpoints"]
    assert "historical" in market_endpoints
