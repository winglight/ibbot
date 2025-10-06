from __future__ import annotations

from typing import Any, Mapping

import pytest
from fastapi import FastAPI, Request, status
from fastapi.testclient import TestClient

from src.routes import logs as logs_module
from src.routes.service_clients import ServiceUnavailableError


class StubLogsServiceClient:
    """Stub for logs service client."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.logs_response: Mapping[str, Any] = {
            "logs": [
                {"timestamp": "2024-01-15T10:00:00Z", "level": "INFO", "message": "Test log"},
                {"timestamp": "2024-01-15T10:01:00Z", "level": "ERROR", "message": "Error log"}
            ],
            "total": 2
        }
        self.modules_response: Mapping[str, Any] = {
            "modules": ["risk", "strategy", "market_data", "orders"]
        }
        self.stats_response: Mapping[str, Any] = {
            "stats": {
                "total_logs": 1000,
                "error_count": 50,
                "warning_count": 100,
                "info_count": 850
            }
        }
        self.should_fail: bool = False

    async def get_logs(self, **kwargs: Any) -> Mapping[str, Any]:
        self.calls.append(("get_logs", kwargs))
        if self.should_fail:
            raise ServiceUnavailableError("Logs service unavailable")
        return self.logs_response

    async def get_modules(self) -> Mapping[str, Any]:
        self.calls.append(("get_modules", None))
        if self.should_fail:
            raise ServiceUnavailableError("Logs service unavailable")
        return self.modules_response

    async def get_stats(self) -> Mapping[str, Any]:
        self.calls.append(("get_stats", None))
        if self.should_fail:
            raise ServiceUnavailableError("Logs service unavailable")
        return self.stats_response


@pytest.fixture()
def logs_app(monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, StubLogsServiceClient]:
    """Create a FastAPI app with logs routes for testing."""
    app = FastAPI()
    app.include_router(logs_module.router)

    async def allow_user(request: Request) -> str:
        request.state.is_read_only = False
        request.state.user_role = "admin"
        request.state.username = "tester"
        return "tester"

    app.dependency_overrides[logs_module.get_current_user] = allow_user

    stub_client = StubLogsServiceClient()
    monkeypatch.setattr(logs_module, "get_logs_service_client", lambda: stub_client)

    return app, stub_client


def test_get_logs_returns_log_entries(logs_app: tuple[FastAPI, StubLogsServiceClient]) -> None:
    """Test that /api/logs returns log entries."""
    app, stub_client = logs_app
    client = TestClient(app)

    response = client.get("/api/logs")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert "logs" in data
    assert "total" in data
    assert len(data["logs"]) == 2
    assert stub_client.calls == [("get_logs", {})]


def test_get_logs_with_query_parameters(logs_app: tuple[FastAPI, StubLogsServiceClient]) -> None:
    """Test that /api/logs accepts and passes query parameters."""
    app, stub_client = logs_app
    client = TestClient(app)

    response = client.get("/api/logs?level=ERROR&module=risk&limit=10&offset=0")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert "logs" in data
    
    # Check that parameters were passed to the service
    assert len(stub_client.calls) == 1
    call_name, call_params = stub_client.calls[0]
    assert call_name == "get_logs"
    assert "level" in call_params
    assert "module" in call_params
    assert "limit" in call_params
    assert "offset" in call_params


def test_get_logs_returns_503_when_service_unavailable(logs_app: tuple[FastAPI, StubLogsServiceClient]) -> None:
    """Test that /api/logs returns 503 when service is unavailable."""
    app, stub_client = logs_app
    stub_client.should_fail = True
    client = TestClient(app)

    response = client.get("/api/logs")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_get_logs_modules_returns_module_list(logs_app: tuple[FastAPI, StubLogsServiceClient]) -> None:
    """Test that /api/logs/modules returns list of available modules."""
    app, stub_client = logs_app
    client = TestClient(app)

    response = client.get("/api/logs/modules")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert "modules" in data
    assert len(data["modules"]) == 4
    assert "risk" in data["modules"]
    assert "strategy" in data["modules"]
    assert stub_client.calls == [("get_modules", None)]


def test_get_logs_modules_returns_503_when_service_unavailable(logs_app: tuple[FastAPI, StubLogsServiceClient]) -> None:
    """Test that /api/logs/modules returns 503 when service is unavailable."""
    app, stub_client = logs_app
    stub_client.should_fail = True
    client = TestClient(app)

    response = client.get("/api/logs/modules")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_get_logs_stats_returns_statistics(logs_app: tuple[FastAPI, StubLogsServiceClient]) -> None:
    """Test that /api/logs/stats returns log statistics."""
    app, stub_client = logs_app
    client = TestClient(app)

    response = client.get("/api/logs/stats")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert "stats" in data
    stats = data["stats"]
    assert "total_logs" in stats
    assert "error_count" in stats
    assert "warning_count" in stats
    assert "info_count" in stats
    assert stats["total_logs"] == 1000
    assert stats["error_count"] == 50
    assert stub_client.calls == [("get_stats", None)]


def test_get_logs_stats_returns_503_when_service_unavailable(logs_app: tuple[FastAPI, StubLogsServiceClient]) -> None:
    """Test that /api/logs/stats returns 503 when service is unavailable."""
    app, stub_client = logs_app
    stub_client.should_fail = True
    client = TestClient(app)

    response = client.get("/api/logs/stats")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_get_logs_with_date_range_parameters(logs_app: tuple[FastAPI, StubLogsServiceClient]) -> None:
    """Test that /api/logs accepts date range parameters."""
    app, stub_client = logs_app
    client = TestClient(app)

    response = client.get("/api/logs?start_date=2024-01-15&end_date=2024-01-16")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert "logs" in data
    
    # Check that date parameters were passed to the service
    assert len(stub_client.calls) == 1
    call_name, call_params = stub_client.calls[0]
    assert call_name == "get_logs"
    assert "start_date" in call_params
    assert "end_date" in call_params


def test_get_logs_response_format(logs_app: tuple[FastAPI, StubLogsServiceClient]) -> None:
    """Test the format of the logs response."""
    app, stub_client = logs_app
    client = TestClient(app)

    response = client.get("/api/logs")

    assert response.status_code == status.HTTP_200_OK
    assert response.headers["content-type"] == "application/json"
    
    data = response.json()
    assert isinstance(data["logs"], list)
    assert isinstance(data["total"], int)
    
    # Check log entry format
    if data["logs"]:
        log_entry = data["logs"][0]
        assert "timestamp" in log_entry
        assert "level" in log_entry
        assert "message" in log_entry