from __future__ import annotations

from typing import Any, Mapping

import pytest
from fastapi import FastAPI, Request, status
from fastapi.testclient import TestClient

from src.routes import strategy as strategies_module
from src.routes.service_clients import ServiceUnavailableError


class StubStrategiesServiceClient:
    """Stub for strategies service client."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.strategies_response: Mapping[str, Any] = {
            "strategies": [
                {
                    "id": "strategy_1",
                    "name": "Mean Reversion Strategy",
                    "status": "active",
                    "created_at": "2024-01-15T10:00:00Z",
                    "updated_at": "2024-01-15T10:00:00Z"
                },
                {
                    "id": "strategy_2",
                    "name": "Momentum Strategy",
                    "status": "inactive",
                    "created_at": "2024-01-15T09:00:00Z",
                    "updated_at": "2024-01-15T09:30:00Z"
                }
            ]
        }
        self.strategy_response: Mapping[str, Any] = {
            "id": "strategy_1",
            "name": "Mean Reversion Strategy",
            "description": "A strategy that trades on mean reversion signals",
            "status": "active",
            "parameters": {
                "lookback_period": 20,
                "threshold": 2.0
            },
            "performance": {
                "total_return": 0.15,
                "sharpe_ratio": 1.2,
                "max_drawdown": -0.05
            },
            "created_at": "2024-01-15T10:00:00Z",
            "updated_at": "2024-01-15T10:00:00Z"
        }
        self.performance_response: Mapping[str, Any] = {
            "performance": {
                "total_return": 0.15,
                "sharpe_ratio": 1.2,
                "max_drawdown": -0.05,
                "win_rate": 0.65,
                "profit_factor": 1.8,
                "daily_returns": [0.001, 0.002, -0.001, 0.003]
            }
        }
        self.should_fail: bool = False

    async def list_strategies(self, **kwargs: Any) -> Mapping[str, Any]:
        self.calls.append(("list_strategies", kwargs))
        if self.should_fail:
            raise ServiceUnavailableError("Strategies service unavailable")
        return self.strategies_response

    async def get_strategy(self, strategy_id: str) -> Mapping[str, Any]:
        self.calls.append(("get_strategy", {"strategy_id": strategy_id}))
        if self.should_fail:
            raise ServiceUnavailableError("Strategies service unavailable")
        return self.strategy_response

    async def create_strategy(self, strategy_data: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append(("create_strategy", strategy_data))
        if self.should_fail:
            raise ServiceUnavailableError("Strategies service unavailable")
        return {**self.strategy_response, **strategy_data}

    async def update_strategy(self, strategy_id: str, strategy_data: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append(("update_strategy", {"strategy_id": strategy_id, **strategy_data}))
        if self.should_fail:
            raise ServiceUnavailableError("Strategies service unavailable")
        return {**self.strategy_response, **strategy_data}

    async def delete_strategy(self, strategy_id: str) -> Mapping[str, Any]:
        self.calls.append(("delete_strategy", {"strategy_id": strategy_id}))
        if self.should_fail:
            raise ServiceUnavailableError("Strategies service unavailable")
        return {"status": "deleted", "strategy_id": strategy_id}

    async def start_strategy(self, strategy_id: str) -> Mapping[str, Any]:
        self.calls.append(("start_strategy", {"strategy_id": strategy_id}))
        if self.should_fail:
            raise ServiceUnavailableError("Strategies service unavailable")
        return {"status": "started", "strategy_id": strategy_id}

    async def stop_strategy(self, strategy_id: str) -> Mapping[str, Any]:
        self.calls.append(("stop_strategy", {"strategy_id": strategy_id}))
        if self.should_fail:
            raise ServiceUnavailableError("Strategies service unavailable")
        return {"status": "stopped", "strategy_id": strategy_id}

    async def get_strategy_performance(self, strategy_id: str, **kwargs: Any) -> Mapping[str, Any]:
        self.calls.append(("get_strategy_performance", {"strategy_id": strategy_id, **kwargs}))
        if self.should_fail:
            raise ServiceUnavailableError("Strategies service unavailable")
        return self.performance_response


@pytest.fixture()
def strategies_app(monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, StubStrategiesServiceClient]:
    """Create a FastAPI app with strategies routes for testing."""
    app = FastAPI()
    app.include_router(strategies_module.router)

    async def allow_user(request: Request) -> str:
        request.state.is_read_only = False
        request.state.user_role = "admin"
        request.state.username = "tester"
        return "tester"

    app.dependency_overrides[strategies_module.get_current_user] = allow_user

    stub_client = StubStrategiesServiceClient()
    monkeypatch.setattr(strategies_module, "get_strategies_service_client", lambda: stub_client)

    return app, stub_client


def test_list_strategies_returns_strategy_list(strategies_app: tuple[FastAPI, StubStrategiesServiceClient]) -> None:
    """Test that /api/strategies returns list of strategies."""
    app, stub_client = strategies_app
    client = TestClient(app)

    response = client.get("/api/strategies")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert "strategies" in data
    assert len(data["strategies"]) == 2
    
    strategy = data["strategies"][0]
    assert "id" in strategy
    assert "name" in strategy
    assert "status" in strategy
    assert stub_client.calls == [("list_strategies", {})]


def test_list_strategies_with_filters(strategies_app: tuple[FastAPI, StubStrategiesServiceClient]) -> None:
    """Test that /api/strategies accepts filter parameters."""
    app, stub_client = strategies_app
    client = TestClient(app)

    response = client.get("/api/strategies?status=active&limit=10&offset=0")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert "strategies" in data
    
    # Check that parameters were passed to the service
    assert len(stub_client.calls) == 1
    call_name, call_params = stub_client.calls[0]
    assert call_name == "list_strategies"
    assert "status" in call_params
    assert "limit" in call_params
    assert "offset" in call_params


def test_list_strategies_returns_503_when_service_unavailable(strategies_app: tuple[FastAPI, StubStrategiesServiceClient]) -> None:
    """Test that /api/strategies returns 503 when service is unavailable."""
    app, stub_client = strategies_app
    stub_client.should_fail = True
    client = TestClient(app)

    response = client.get("/api/strategies")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_get_strategy_returns_strategy_details(strategies_app: tuple[FastAPI, StubStrategiesServiceClient]) -> None:
    """Test that /api/strategies/{strategy_id} returns strategy details."""
    app, stub_client = strategies_app
    client = TestClient(app)

    response = client.get("/api/strategies/strategy_1")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["id"] == "strategy_1"
    assert data["name"] == "Mean Reversion Strategy"
    assert "description" in data
    assert "parameters" in data
    assert "performance" in data
    assert stub_client.calls == [("get_strategy", {"strategy_id": "strategy_1"})]


def test_get_strategy_returns_503_when_service_unavailable(strategies_app: tuple[FastAPI, StubStrategiesServiceClient]) -> None:
    """Test that /api/strategies/{strategy_id} returns 503 when service is unavailable."""
    app, stub_client = strategies_app
    stub_client.should_fail = True
    client = TestClient(app)

    response = client.get("/api/strategies/strategy_1")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_create_strategy_creates_new_strategy(strategies_app: tuple[FastAPI, StubStrategiesServiceClient]) -> None:
    """Test that POST /api/strategies creates a new strategy."""
    app, stub_client = strategies_app
    client = TestClient(app)

    strategy_data = {
        "name": "New Strategy",
        "description": "A new trading strategy",
        "parameters": {"param1": "value1"}
    }

    response = client.post("/api/strategies", json=strategy_data)

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["name"] == "New Strategy"
    assert data["description"] == "A new trading strategy"
    assert stub_client.calls == [("create_strategy", strategy_data)]


def test_create_strategy_returns_503_when_service_unavailable(strategies_app: tuple[FastAPI, StubStrategiesServiceClient]) -> None:
    """Test that POST /api/strategies returns 503 when service is unavailable."""
    app, stub_client = strategies_app
    stub_client.should_fail = True
    client = TestClient(app)

    response = client.post("/api/strategies", json={"name": "Test Strategy"})

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_update_strategy_modifies_existing_strategy(strategies_app: tuple[FastAPI, StubStrategiesServiceClient]) -> None:
    """Test that PUT /api/strategies/{strategy_id} updates an existing strategy."""
    app, stub_client = strategies_app
    client = TestClient(app)

    update_data = {
        "name": "Updated Strategy",
        "parameters": {"param1": "new_value"}
    }

    response = client.put("/api/strategies/strategy_1", json=update_data)

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["name"] == "Updated Strategy"
    
    # Check that the update was called with correct parameters
    assert len(stub_client.calls) == 1
    call_name, call_params = stub_client.calls[0]
    assert call_name == "update_strategy"
    assert call_params["strategy_id"] == "strategy_1"
    assert call_params["name"] == "Updated Strategy"


def test_update_strategy_returns_503_when_service_unavailable(strategies_app: tuple[FastAPI, StubStrategiesServiceClient]) -> None:
    """Test that PUT /api/strategies/{strategy_id} returns 503 when service is unavailable."""
    app, stub_client = strategies_app
    stub_client.should_fail = True
    client = TestClient(app)

    response = client.put("/api/strategies/strategy_1", json={"name": "Updated"})

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_delete_strategy_removes_strategy(strategies_app: tuple[FastAPI, StubStrategiesServiceClient]) -> None:
    """Test that DELETE /api/strategies/{strategy_id} deletes a strategy."""
    app, stub_client = strategies_app
    client = TestClient(app)

    response = client.delete("/api/strategies/strategy_1")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["status"] == "deleted"
    assert data["strategy_id"] == "strategy_1"
    assert stub_client.calls == [("delete_strategy", {"strategy_id": "strategy_1"})]


def test_delete_strategy_returns_503_when_service_unavailable(strategies_app: tuple[FastAPI, StubStrategiesServiceClient]) -> None:
    """Test that DELETE /api/strategies/{strategy_id} returns 503 when service is unavailable."""
    app, stub_client = strategies_app
    stub_client.should_fail = True
    client = TestClient(app)

    response = client.delete("/api/strategies/strategy_1")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_start_strategy_activates_strategy(strategies_app: tuple[FastAPI, StubStrategiesServiceClient]) -> None:
    """Test that POST /api/strategies/{strategy_id}/start activates a strategy."""
    app, stub_client = strategies_app
    client = TestClient(app)

    response = client.post("/api/strategies/strategy_1/start")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["status"] == "started"
    assert data["strategy_id"] == "strategy_1"
    assert stub_client.calls == [("start_strategy", {"strategy_id": "strategy_1"})]


def test_start_strategy_returns_503_when_service_unavailable(strategies_app: tuple[FastAPI, StubStrategiesServiceClient]) -> None:
    """Test that POST /api/strategies/{strategy_id}/start returns 503 when service is unavailable."""
    app, stub_client = strategies_app
    stub_client.should_fail = True
    client = TestClient(app)

    response = client.post("/api/strategies/strategy_1/start")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_stop_strategy_deactivates_strategy(strategies_app: tuple[FastAPI, StubStrategiesServiceClient]) -> None:
    """Test that POST /api/strategies/{strategy_id}/stop deactivates a strategy."""
    app, stub_client = strategies_app
    client = TestClient(app)

    response = client.post("/api/strategies/strategy_1/stop")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["status"] == "stopped"
    assert data["strategy_id"] == "strategy_1"
    assert stub_client.calls == [("stop_strategy", {"strategy_id": "strategy_1"})]


def test_stop_strategy_returns_503_when_service_unavailable(strategies_app: tuple[FastAPI, StubStrategiesServiceClient]) -> None:
    """Test that POST /api/strategies/{strategy_id}/stop returns 503 when service is unavailable."""
    app, stub_client = strategies_app
    stub_client.should_fail = True
    client = TestClient(app)

    response = client.post("/api/strategies/strategy_1/stop")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_get_strategy_performance_returns_metrics(strategies_app: tuple[FastAPI, StubStrategiesServiceClient]) -> None:
    """Test that /api/strategies/{strategy_id}/performance returns performance metrics."""
    app, stub_client = strategies_app
    client = TestClient(app)

    response = client.get("/api/strategies/strategy_1/performance")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert "performance" in data
    performance = data["performance"]
    assert "total_return" in performance
    assert "sharpe_ratio" in performance
    assert "max_drawdown" in performance
    assert "win_rate" in performance
    assert stub_client.calls == [("get_strategy_performance", {"strategy_id": "strategy_1"})]


def test_get_strategy_performance_with_date_range(strategies_app: tuple[FastAPI, StubStrategiesServiceClient]) -> None:
    """Test that /api/strategies/{strategy_id}/performance accepts date range parameters."""
    app, stub_client = strategies_app
    client = TestClient(app)

    response = client.get("/api/strategies/strategy_1/performance?start_date=2024-01-01&end_date=2024-01-31")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert "performance" in data
    
    # Check that parameters were passed to the service
    assert len(stub_client.calls) == 1
    call_name, call_params = stub_client.calls[0]
    assert call_name == "get_strategy_performance"
    assert call_params["strategy_id"] == "strategy_1"
    assert "start_date" in call_params
    assert "end_date" in call_params


def test_get_strategy_performance_returns_503_when_service_unavailable(strategies_app: tuple[FastAPI, StubStrategiesServiceClient]) -> None:
    """Test that /api/strategies/{strategy_id}/performance returns 503 when service is unavailable."""
    app, stub_client = strategies_app
    stub_client.should_fail = True
    client = TestClient(app)

    response = client.get("/api/strategies/strategy_1/performance")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE