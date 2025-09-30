from datetime import datetime
from http import HTTPStatus
import json

import pytest

import core
from src.queue_manager import QueueManager, QueueTask
from src.utils import now_local


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


def test_queue_all_tasks_snapshot(client):
    first = core.queue_manager.add_task("alpha", {"payload": 1})
    second = core.queue_manager.add_task("beta", {"payload": 2})

    try:
        response = client.get("/api/queue/tasks")
        assert response.status_code == HTTPStatus.OK
        payload = response.json()
        assert "tasks" in payload
        tasks = payload["tasks"]
        assert isinstance(tasks, list)

        seen_ids = {task.get("task_id") for task in tasks}
        assert {first, second}.issubset(seen_ids)
    finally:
        for task_id in (first, second):
            core.queue_manager.tasks.pop(task_id, None)
            if task_id in core.queue_manager.pending_queue:
                core.queue_manager.pending_queue.remove(task_id)


@pytest.mark.asyncio
async def test_queue_persistence_flush_and_reload(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    manager = QueueManager()
    manager.tasks.clear()
    manager.pending_queue.clear()
    monkeypatch.setattr(manager, "_resolve_notification_manager", lambda: None)

    sample_task = QueueTask(
        task_id="persist-test",
        task_type="unit-test",
        payload={"value": 1},
        status="pending",
        created_at=now_local(),
    )
    manager.tasks[sample_task.task_id] = sample_task

    await manager.start_worker()
    try:
        sample_task.status = "completed"
        sample_task.completed_at = now_local()
        manager.pending_queue.clear()
        manager._schedule_tasks_persistence()
    finally:
        await manager.stop_worker()

    assert manager.queue_file.exists()
    queue_snapshot = json.loads(manager.queue_file.read_text(encoding="utf-8"))
    assert queue_snapshot["pending_queue"] == []
    assert queue_snapshot["tasks"][sample_task.task_id]["status"] == "completed"

    manager.tasks.clear()
    manager.pending_queue.clear()
    manager._load_tasks()

    assert sample_task.task_id in manager.tasks
    reloaded_task = manager.tasks[sample_task.task_id]
    assert reloaded_task.status == "completed"
    assert isinstance(reloaded_task.completed_at, datetime)
    assert manager.pending_queue == []
