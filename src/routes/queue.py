"""Queue metrics routes backed by the queue monitor snapshot."""

from fastapi import APIRouter, HTTPException

from .queue_monitor import QueueMonitorClient


router = APIRouter()

_QUEUE_MONITOR_CLIENT = QueueMonitorClient()


def set_queue_monitor_client(client: QueueMonitorClient | None) -> None:
    """Override the global queue monitor client (mainly for tests)."""

    global _QUEUE_MONITOR_CLIENT
    _QUEUE_MONITOR_CLIENT = client or QueueMonitorClient()


def _get_client() -> QueueMonitorClient:
    return _QUEUE_MONITOR_CLIENT


@router.get("/api/queue/status")
async def get_queue_status() -> dict[str, object]:
    """Return the queue monitor snapshot with live metrics."""

    snapshot = await _get_client().fetch_snapshot()
    if snapshot is None:
        raise HTTPException(status_code=503, detail="Queue monitor snapshot unavailable")
    return snapshot


@router.get("/api/queue/task/{queue_id}")
async def get_task_status(queue_id: str) -> dict[str, object]:
    """Return metrics for a specific monitored queue."""

    snapshot = await _get_client().fetch_snapshot()
    if snapshot is None:
        raise HTTPException(status_code=503, detail="Queue monitor snapshot unavailable")
    entry = QueueMonitorClient.find_queue(snapshot, queue_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Queue not found")
    return {"timestamp": snapshot.get("timestamp"), "queue": entry}


@router.get("/api/queue/tasks")
async def get_all_tasks() -> dict[str, object]:
    """Return metrics for all monitored queues."""

    snapshot = await _get_client().fetch_snapshot()
    if snapshot is None:
        return {
            "timestamp": None,
            "queues": [],
            "detail": "Queue monitor snapshot unavailable",
        }
    return {"timestamp": snapshot.get("timestamp"), "queues": snapshot.get("queues", [])}
