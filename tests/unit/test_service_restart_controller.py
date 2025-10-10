import asyncio
from pathlib import Path

import pytest

from src.service_restart import DockerComposeController, RestartCommandResult


class RecordingDockerController(DockerComposeController):
    def __init__(self, project_dir: Path) -> None:
        super().__init__(
            compose_command=("docker", "compose"),
            project_dir=project_dir,
        )
        self.calls: list[tuple[tuple[str, ...], str]] = []

    async def _run(self, args: tuple[str, ...], service: str) -> RestartCommandResult:  # type: ignore[override]
        self.calls.append((tuple(args), service))
        command = (*self._command, *args)
        return RestartCommandResult(
            service=service,
            command=command,
            returncode=0,
            stdout="",
            stderr="",
            mode=self.mode,
        )


@pytest.mark.parametrize(
    ("slug", "expected"),
    (
        ("ib_command", "ib-command"),
        ("market_data", "market-data"),
        ("market_data_feed", "ib-feed"),
        ("queue_monitor", "queue-monitor"),
    ),
)
def test_docker_controller_maps_service_names(tmp_path: Path, slug: str, expected: str) -> None:
    controller = RecordingDockerController(project_dir=tmp_path)

    result = asyncio.run(controller.restart(slug))

    assert controller.calls == [(("restart", expected), slug)]
    assert result.command[-1] == expected
    assert result.service == slug


def test_docker_controller_preserves_unknown_service(tmp_path: Path) -> None:
    controller = RecordingDockerController(project_dir=tmp_path)

    result = asyncio.run(controller.restart("orders"))

    assert controller.calls == [(("restart", "orders"), "orders")]
    assert result.command[-1] == "orders"
    assert result.service == "orders"
