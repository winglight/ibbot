import os
import time

os.environ.setdefault("IBBOT_SKIP_SERVER", "1")
os.environ.setdefault("OPTIMIZER_DB_PATH", ":memory:")

from pathlib import Path

from fastapi.testclient import TestClient

from optimizer_main import app
from src.optimizer_service import strategy_generator


def wait_for_report(client: TestClient, task_id: str, timeout: float = 2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/api/optimize/report/{task_id}")
        if response.status_code == 200:
            payload = response.json()
            if payload.get("status") == "ok" and payload.get("report"):
                return payload["report"]
        time.sleep(0.05)
    raise AssertionError("Report not ready within timeout")


def sample_payload():
    return {
        "strategy": "test_strategy",
        "start_date": "2023-01-01",
        "end_date": "2023-03-01",
        "parameters": {"window": [10, 20], "threshold": [0.1, 0.2]},
        "algorithm": "grid",
    }


def test_metadata_endpoint():
    with TestClient(app) as client:
        response = client.get("/api/optimize/metadata")
        assert response.status_code == 200
        payload = response.json()
        assert set(payload.keys()) == {"base_metrics", "feature_metrics", "algorithms"}
        assert "obi5" in payload["base_metrics"]
        assert "stacking" in payload["feature_metrics"]
        assert "grid" in payload["algorithms"]


def test_data_range_endpoint():
    with TestClient(app) as client:
        response = client.get("/api/optimize/data-range", params={"symbol": "MNQ"})
        assert response.status_code == 200
        payload = response.json()
        assert payload["symbol"] == "MNQ"
        assert payload["start_date"] < payload["end_date"]


def test_start_returns_task_id():
    with TestClient(app) as client:
        response = client.post("/api/optimize/start", json=sample_payload())
        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "ok"
        assert isinstance(data["task_id"], str)


def test_status_and_report_flow():
    with TestClient(app) as client:
        response = client.post("/api/optimize/start", json=sample_payload())
        task_id = response.json()["task_id"]

        status_resp = client.get(f"/api/optimize/status/{task_id}")
        assert status_resp.status_code == 200
        status_payload = status_resp.json()
        assert status_payload["status"] == "ok"
        assert status_payload["task_id"] == task_id
        assert status_payload["state"] in {"running", "completed"}

        report = wait_for_report(client, task_id)
        summary = report["summary"]
        results = report["results"]
        assert isinstance(results, list)
        assert summary["successful_combinations"] == len(results)
        assert summary["tested_combinations"] == summary["successful_combinations"] + len(report.get("fail_reasons", []))
        assert all("metrics" in item and "parameters" in item for item in results)


def test_report_missing_task_returns_404():
    with TestClient(app) as client:
        response = client.get("/api/optimize/report/nonexistent")
        assert response.status_code == 404
        assert response.json()["detail"] == "task not found"


def test_invalid_iterations_for_grid_search():
    payload = sample_payload()
    payload["iterations"] = 10
    with TestClient(app) as client:
        response = client.post("/api/optimize/start", json=payload)
        assert response.status_code == 422


def test_random_algorithm_uses_iterations():
    payload = sample_payload()
    payload["algorithm"] = "random"
    payload["iterations"] = 5
    with TestClient(app) as client:
        response = client.post("/api/optimize/start", json=payload)
        assert response.status_code == 202
        report = wait_for_report(client, response.json()["task_id"])
        summary = report["summary"]
        assert summary["tested_combinations"] == payload["iterations"]
        assert summary["successful_combinations"] <= payload["iterations"]


def test_grid_algorithm_tests_cartesian_product():
    payload = sample_payload()
    with TestClient(app) as client:
        response = client.post("/api/optimize/start", json=payload)
        assert response.status_code == 202
        report = wait_for_report(client, response.json()["task_id"])
        summary = report["summary"]
        expected_total = len(payload["parameters"]["window"]) * len(payload["parameters"]["threshold"])
        assert summary["tested_combinations"] == expected_total


def test_bayesian_algorithm_respects_iterations():
    payload = sample_payload()
    payload["algorithm"] = "bayesian"
    payload["iterations"] = 6
    with TestClient(app) as client:
        response = client.post("/api/optimize/start", json=payload)
        assert response.status_code == 202
        report = wait_for_report(client, response.json()["task_id"])
        summary = report["summary"]
        assert summary["tested_combinations"] == payload["iterations"]


def test_generate_strategy_endpoint(monkeypatch, tmp_path):
    monkeypatch.setattr(strategy_generator, "STRATEGY_DIR", tmp_path)
    payload = {
        "strategy_name": "Auto Strategy",
        "base_parameters": {"long_thresholds": 0.8},
        "feature_parameters": {"stacking": {"intensity": 0.3}},
        "plan_id": "plan-1",
        "task_id": "task-1",
    }
    with TestClient(app) as client:
        response = client.post("/api/optimize/strategies", json=payload)
        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "ok"
        generated_path = Path(data["file_path"])
        assert generated_path.exists()
        content = generated_path.read_text(encoding="utf-8")
        assert "Auto Strategy" in content
        assert "BASE_PARAMETERS" in content
