"""Tests for the service restart dependency planner."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Sequence

from src.service_restart import (
    RestartCommandResult,
    RestartMode,
    ServiceRestartManager,
    DockerComposeController,
)


class DummyController:
    """Minimal controller used to instantiate the restart manager."""

    mode = RestartMode.LOCAL

    async def restart(self, service: str) -> Any:  # pragma: no cover - unused
        raise AssertionError(f"restart should not be called for {service}")


def test_notification_restart_plan_has_no_dependencies() -> None:
    """Restarting notification should no longer touch connection_control."""

    manager = ServiceRestartManager(DummyController())

    plan = manager.resolve_plan("notification")

    assert plan == ["notification"]


def test_market_data_restart_plan_includes_feed_dependency() -> None:
    """Market data restarts must include the feed dependency first."""

    manager = ServiceRestartManager(DummyController())

    plan = manager.resolve_plan("market_data")

    assert plan == ["market_data_feed", "market_data"]


class StubDockerController(DockerComposeController):
    """Stub compose controller that records restart targets."""

    def __init__(self) -> None:
        super().__init__(
            compose_command=("docker", "compose"),
            project_dir=Path(__file__).resolve().parents[1] / "docker",
        )
        self.calls: list[tuple[tuple[str, ...], str]] = []

    async def _run(self, args: Sequence[str], service: str) -> RestartCommandResult:  # type: ignore[override]
        command = (*self._command, *args)
        self.calls.append((tuple(args), service))
        return RestartCommandResult(
            service=service,
            command=command,
            returncode=0,
            stdout="ok",
            stderr="",
            mode=self.mode,
        )


def test_compose_controller_maps_service_names() -> None:
    async def _run() -> None:
        controller = StubDockerController()

        await controller.restart("market_data_feed")
        await controller.restart("market_data")
        await controller.restart("queue_monitor")

        targets = [call[0][-1] for call in controller.calls]
        assert targets == ["ib-feed", "market-data", "queue-monitor"]

    asyncio.run(_run())
