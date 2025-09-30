"""Utilities for orchestrating Docker-based service restarts."""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


@dataclass(slots=True)
class DockerCommandResult:
    """Result from executing a docker-compose command."""

    service: str
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class UnknownServiceError(ValueError):
    """Raised when attempting to restart an unknown service name."""

    def __init__(self, service: str) -> None:
        super().__init__(f"Unknown service '{service}'")
        self.service = service


class DockerExecutionError(RuntimeError):
    """Raised when the docker-compose command exits with a failure."""

    def __init__(self, service: str, result: DockerCommandResult) -> None:
        super().__init__(
            f"Command for service '{service}' exited with status {result.returncode}"
        )
        self.service = service
        self.result = result


def _default_compose_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "docker"


class DockerComposeController:
    """Minimal docker-compose wrapper suitable for mocking in tests."""

    def __init__(
        self,
        *,
        compose_command: Sequence[str] | None = None,
        project_dir: Path | str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        default_cmd = os.getenv("SERVICE_RESTART_COMPOSE_CMD")
        if compose_command is None:
            compose_command = shlex.split(default_cmd) if default_cmd else ("docker", "compose")
        self._command = tuple(compose_command)

        root_dir = os.getenv("SERVICE_RESTART_PROJECT_DIR")
        if project_dir is None:
            project_dir = Path(root_dir) if root_dir else _default_compose_dir()
        self._project_dir = Path(project_dir)

        base_env = dict(os.environ)
        if env:
            base_env.update(env)
        compose_file = os.getenv("SERVICE_RESTART_COMPOSE_FILE")
        if compose_file:
            base_env.setdefault("COMPOSE_FILE", compose_file)
        self._env = base_env

    async def _run(self, args: Sequence[str], service: str) -> DockerCommandResult:
        command = (*self._command, *args)

        def _execute() -> DockerCommandResult:
            completed = subprocess.run(
                command,
                cwd=str(self._project_dir),
                env=self._env,
                capture_output=True,
                text=True,
                check=False,
            )
            return DockerCommandResult(
                service=service,
                command=tuple(command),
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )

        return await asyncio.to_thread(_execute)

    async def restart(self, service: str) -> DockerCommandResult:
        return await self._run(("restart", service), service)


DEFAULT_SERVICE_DEPENDENCIES: Mapping[str, Sequence[str]] = {
    "ib-gateway": (),
    "ibbot": ("ib-gateway",),
    "optimizer": ("ibbot",),
    "cloudflared": ("ibbot",),
}


class ServiceRestartManager:
    """Determine restart order and delegate to the docker controller."""

    def __init__(
        self,
        docker: DockerComposeController,
        *,
        dependencies: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        mapping = dependencies or DEFAULT_SERVICE_DEPENDENCIES
        self._dependencies: dict[str, tuple[str, ...]] = {
            name: tuple(deps) for name, deps in mapping.items()
        }
        self._docker = docker

    def available_services(self) -> tuple[str, ...]:
        return tuple(self._dependencies.keys())

    def resolve_plan(self, service: str, *, include_dependencies: bool = True) -> list[str]:
        if service not in self._dependencies:
            raise UnknownServiceError(service)
        if not include_dependencies:
            return [service]

        ordered: list[str] = []
        visited: set[str] = set()

        def visit(name: str) -> None:
            if name in visited:
                return
            visited.add(name)
            for dependency in self._dependencies.get(name, ()):  # pragma: no cover - defensive
                visit(dependency)
            if name not in ordered:
                ordered.append(name)

        visit(service)
        return ordered

    async def restart(
        self, service: str, *, include_dependencies: bool = True
    ) -> list[DockerCommandResult]:
        plan = self.resolve_plan(service, include_dependencies=include_dependencies)
        results: list[DockerCommandResult] = []
        for name in plan:
            result = await self._docker.restart(name)
            if result.returncode != 0:
                raise DockerExecutionError(name, result)
            results.append(result)
        return results


_manager: ServiceRestartManager | None = None


def get_service_restart_manager() -> ServiceRestartManager:
    """Return the singleton restart manager used by the API layer."""

    global _manager
    if _manager is None:
        docker = DockerComposeController()
        _manager = ServiceRestartManager(docker)
    return _manager


def set_service_restart_manager(manager: ServiceRestartManager | None) -> None:
    """Override the global restart manager (primarily for tests)."""

    global _manager
    _manager = manager

