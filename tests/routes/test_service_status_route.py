from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.routes import service as service_module


@pytest.fixture()
def service_status_app(monkeypatch: pytest.MonkeyPatch):
    app = FastAPI()
    app.include_router(service_module.router)

    async def _fake_build_status_payload(**_: object) -> dict[str, object]:
        return {
            "generated_at": "2024-01-01T00:00:00Z",
            "services": [],
            "registry": [
                {
                    "service": "ibbot",
                    "display_name": "Trading Core",
                    "internal": True,
                    "health_key": None,
                }
            ],
        }

    monkeypatch.setattr(
        service_module,
        "build_service_status_payload",
        _fake_build_status_payload,
    )

    return app


def test_get_service_status_returns_registry(service_status_app):
    with TestClient(service_status_app) as client:
        response = client.get("/api/services/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload.get("registry") == [
        {
            "service": "ibbot",
            "display_name": "Trading Core",
            "internal": True,
            "health_key": None,
        }
    ]
    assert "generated_at" in payload
