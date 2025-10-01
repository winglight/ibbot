"""Tests for IB command service health evaluation."""

from src import ib_command_service as command_service


def test_ib_command_service_idle_status_considered_connected():
    now = command_service.now_local().isoformat()

    connected, payload, error = command_service.evaluate_service_health(
        {"status": "idle", "last_seen": now, "updated_at": now},
        stale_seconds=60.0,
    )

    assert connected is True
    assert error is None
    assert payload["status"] == "idle"
    assert payload.get("age_seconds") is not None
