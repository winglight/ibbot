import os
import time

os.environ.setdefault("IBBOT_SKIP_SERVER", "1")
os.environ.setdefault("OPTIMIZER_DB_PATH", ":memory:")

from fastapi.testclient import TestClient

from optimizer_main import app


def plan_payload(frequency_minutes: int = 0):
    return {
        "name": "MNQ日内优化",
        "strategy": "mnq_dom",
        "symbol": "MNQ",
        "base_metrics": ["obi5", "ofi", "trend_score"],
        "feature_metrics": ["stacking", "sweep"],
        "parameters": {
            "base": {
                "long_thresholds": [0.75, 0.85],
                "short_thresholds": [-0.75, -0.85],
                "window": [8000, 10000],
                "max_levels": [20, 30],
                "depth_rows": [5, 10],
            },
            "features": {
                "stacking": {"intensity": [0.3, 0.4]},
                "sweep": {"intensity": [0.6, 0.7]},
            },
        },
        "algorithm": "grid",
        "start_date": "2023-01-01",
        "end_date": "2023-02-01",
        "frequency_minutes": frequency_minutes,
    }


def wait_for_plan_reports(client: TestClient, plan_id: str, timeout: float = 3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/api/optimize/plans/{plan_id}/reports")
        response.raise_for_status()
        data = response.json()
        if data["reports"]:
            if data["reports"][0]["state"] == "running":
                time.sleep(0.05)
                continue
            return data["reports"]
        time.sleep(0.05)
    raise AssertionError("Plan reports not available within timeout")


def test_plan_lifecycle():
    with TestClient(app) as client:
        response = client.post("/api/optimize/plans", json=plan_payload())
        assert response.status_code == 201
        plan = response.json()
        plan_id = plan["id"]
        assert plan["is_active"] is False
        assert plan["parameters"]["base"]["long_thresholds"] == [0.75, 0.85]
        assert plan["parameters"]["base"]["window"] == [8000, 10000]
        assert plan["parameters"]["base"]["max_levels"] == [20, 30]
        assert plan["parameters"]["base"]["depth_rows"] == [5, 10]
        assert plan["base_metrics"] == ["obi5", "ofi", "trend_score"]
        assert plan["feature_metrics"] == ["stacking", "sweep"]
        assert set(plan["metrics"]) == set(plan["base_metrics"] + plan["feature_metrics"])
        assert "stacking" in plan["parameters"]["features"]

        update_resp = client.patch(
            f"/api/optimize/plans/{plan_id}",
            json={
                "name": "MNQ计划-更新",
                "frequency_minutes": 15,
                "base_metrics": ["obi5", "ofi"],
                "feature_metrics": ["stacking"],
            },
        )
        assert update_resp.status_code == 200
        updated_plan = update_resp.json()
        assert updated_plan["name"] == "MNQ计划-更新"
        assert updated_plan["base_metrics"] == ["obi5", "ofi"]
        assert updated_plan["feature_metrics"] == ["stacking"]

        start_resp = client.post(f"/api/optimize/plans/{plan_id}/start")
        assert start_resp.status_code == 200
        reports = wait_for_plan_reports(client, plan_id)
        assert reports[0]["state"] in {"completed", "failed"}

        forbidden_resp = client.patch(
            f"/api/optimize/plans/{plan_id}",
            json={"name": "不应成功"},
        )
        assert forbidden_resp.status_code == 400
        assert forbidden_resp.json()["detail"] == "plan must be paused before editing"

        run_resp = client.post(f"/api/optimize/plans/{plan_id}/run")
        assert run_resp.status_code == 200
        reports = wait_for_plan_reports(client, plan_id)
        assert len(reports) >= 2

        stop_resp = client.post(f"/api/optimize/plans/{plan_id}/stop")
        assert stop_resp.status_code == 200
        assert stop_resp.json()["plan"]["is_active"] is False

        del_resp = client.delete(f"/api/optimize/plans/{plan_id}")
        assert del_resp.status_code == 204
        get_resp = client.get(f"/api/optimize/plans/{plan_id}")
        assert get_resp.status_code == 404
