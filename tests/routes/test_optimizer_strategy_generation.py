import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.optimizer_service import routes as optimizer_routes


@pytest.fixture()
def optimizer_app(monkeypatch: pytest.MonkeyPatch):
    app = FastAPI()
    app.include_router(optimizer_routes.router)

    class StubStrategyClient:
        def __init__(self) -> None:
            self.payloads: list[dict[str, object]] = []

        async def create_strategy(self, payload: dict[str, object]) -> dict[str, object]:
            self.payloads.append(payload)
            return {"id": "generated"}

    client_stub = StubStrategyClient()
    monkeypatch.setattr(optimizer_routes, "_STRATEGY_CLIENT", client_stub)

    captured_calls: list[dict[str, object]] = []

    def fake_generate_strategy_file(**kwargs: object) -> tuple[str, str]:
        captured_calls.append(kwargs)
        return ("generated/strategies/demo.py", "GeneratedStrategy")

    monkeypatch.setattr(
        optimizer_routes, "generate_strategy_file", fake_generate_strategy_file
    )

    return app, client_stub, captured_calls


def test_generate_strategy_persists_via_service(optimizer_app) -> None:
    app, client_stub, captured_calls = optimizer_app

    with TestClient(app) as client:
        response = client.post(
            "/api/optimize/strategies",
            json={
                "strategy_name": "AutoDemo",
                "base_parameters": {"window": 5},
                "feature_parameters": {"threshold": 0.2},
                "plan_id": "plan-1",
                "task_id": "task-42",
            },
        )

    assert response.status_code == 201
    payload_json = response.json()
    assert payload_json["file_path"] == "generated/strategies/demo.py"
    assert payload_json["class_name"] == "GeneratedStrategy"

    assert captured_calls and captured_calls[0]["strategy_name"] == "AutoDemo"
    assert client_stub.payloads
    payload = client_stub.payloads[0]
    assert payload["name"] == "AutoDemo"
    assert payload["file_path"] == "generated/strategies/demo.py"
    assert payload["description"]
    assert "计划: plan-1" in payload["description"]
    assert "任务: task-42" in payload["description"]
