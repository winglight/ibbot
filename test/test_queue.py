from http import HTTPStatus

import core


def test_queue_status_endpoint(client):
    response = client.get("/api/queue/status")
    assert response.status_code == HTTPStatus.OK
    data = response.json()
    assert {
        "is_running",
        "pending_count",
        "processing_count",
        "completed_count",
        "failed_count",
        "total_tasks",
        "max_concurrent_tasks",
    } <= data.keys()


def test_queue_task_lookup(client):
    task_id = core.queue_manager.add_task("unit-test", {"value": 42})
    try:
        response = client.get(f"/api/queue/task/{task_id}")
        assert response.status_code == HTTPStatus.OK
        payload = response.json()
        assert payload["task_id"] == task_id
        assert payload["task_type"] == "unit-test"
        assert payload["status"] == "pending"
    finally:
        core.queue_manager.tasks.pop(task_id, None)
        if task_id in core.queue_manager.pending_queue:
            core.queue_manager.pending_queue.remove(task_id)
