"""Tests for the service restart dependency planner."""

from __future__ import annotations

from typing import Any

from src.service_restart import RestartMode, ServiceRestartManager


class DummyController:
    """Minimal controller used to instantiate the restart manager."""

    mode = RestartMode.LOCAL

    async def restart(self, service: str) -> Any:  # pragma: no cover - unused
        raise AssertionError(f"restart should not be called for {service}")


def test_notification_restart_plan_includes_connection_control() -> None:
    """Restarting notification also restarts the connection controller."""

    manager = ServiceRestartManager(DummyController())

    plan = manager.resolve_plan("notification")

    assert plan == ["connection_control", "notification"]
