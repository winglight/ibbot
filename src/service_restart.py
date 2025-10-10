"""Utilities for orchestrating service restarts across multiple runtimes."""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping, Sequence


__all__ = [
    "RestartMode",
    "RestartCommandResult",
    "UnknownServiceError",
    "CommandExecutionError",
    "DockerComposeController",
    "LocalSupervisorController",
    "ServiceRestartManager",
    "get_service_restart_manager",
    "set_service_restart_manager",
]


class RestartMode(str, Enum):
    """Describe the strategy used to restart services."""

    DOCKER = "docker"
    LOCAL = "local"


@dataclass(slots=True)
class RestartCommandResult:
    """Result from executing a restart command."""

    service: str
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    mode: RestartMode


class UnknownServiceError(ValueError):
    """Raised when attempting to restart an unknown service name."""

    def __init__(self, service: str) -> None:
        super().__init__(f"Unknown service '{service}'")
        self.service = service


class CommandExecutionError(RuntimeError):
    """Raised when the restart command exits with a failure."""

    def __init__(self, service: str, result: RestartCommandResult) -> None:
        super().__init__(
            f"Command for service '{service}' exited with status {result.returncode}"
        )
        self.service = service
        self.result = result


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _default_compose_dir() -> Path:
    return _project_root() / "docker"


_DEFAULT_SERVICE_NAME_MAP: Mapping[str, str] = {
    "ib_command": "ib-command",
    "market_data_feed": "ib-feed",
    "market_data": "market-data",
    "queue_monitor": "queue-monitor",
}


class DockerComposeController:
    """Minimal docker-compose wrapper suitable for mocking in tests."""

    mode: RestartMode = RestartMode.DOCKER

    def __init__(
        self,
        *,
        compose_command: Sequence[str] | None = None,
        project_dir: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        service_map: Mapping[str, str] | None = None,
    ) -> None:
        default_cmd = os.getenv("SERVICE_RESTART_COMPOSE_CMD")
        if compose_command is None:
            compose_command = (
                tuple(shlex.split(default_cmd))
                if default_cmd
                else ("docker", "compose")
            )
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
        mapping = dict(_DEFAULT_SERVICE_NAME_MAP)
        if service_map:
            mapping.update(service_map)
        self._service_map = mapping

    def _resolve_service_name(self, service: str) -> str:
        return self._service_map.get(service, service)

    def is_available(self) -> bool:
        """Return ``True`` when docker compose tooling appears to be present."""

        command = self._command[0] if self._command else None
        if not command:
            return False
        if shutil.which(command) is None and not Path(command).exists():
            return False
        return self._project_dir.exists()

    async def _run(self, args: Sequence[str], service: str) -> RestartCommandResult:
        command = (*self._command, *args)

        def _execute() -> RestartCommandResult:
            completed = subprocess.run(
                command,
                cwd=str(self._project_dir),
                env=self._env,
                capture_output=True,
                text=True,
                check=False,
            )
            return RestartCommandResult(
                service=service,
                command=tuple(command),
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                mode=self.mode,
            )

        return await asyncio.to_thread(_execute)

    async def restart(self, service: str) -> RestartCommandResult:
        target = self._resolve_service_name(service)
        return await self._run(("restart", target), service)


class LocalSupervisorController:
    """Fallback controller that invokes repository supervisor scripts."""

    mode: RestartMode = RestartMode.LOCAL

    def __init__(
        self,
        *,
        command_template: Sequence[str] | None = None,
        script_path: Path | str | None = None,
        python_executable: str | None = None,
        env: Mapping[str, str] | None = None,
        working_dir: Path | str | None = None,
    ) -> None:
        default_cmd = os.getenv("SERVICE_RESTART_LOCAL_CMD")
        if command_template is None:
            if default_cmd:
                command_template = shlex.split(default_cmd)
            else:
                script = Path(script_path) if script_path else _project_root() / "scripts" / "run_service.py"
                interpreter = python_executable or os.getenv("SERVICE_RESTART_PYTHON") or sys.executable
                command_template = (interpreter, str(script), "{service}")
        self._template = tuple(command_template)

        self._env = dict(os.environ)
        if env:
            self._env.update(env)

        if working_dir is None:
            working_dir = _project_root()
        self._working_dir = Path(working_dir)

    def _build_command(self, service: str) -> tuple[str, ...]:
        return tuple(part.format(service=service) for part in self._template)

    def is_available(self) -> bool:
        """Check whether the configured command is runnable."""

        if not self._template:
            return False
        first = self._template[0]
        if "{service}" in first:
            # we cannot validate until a service is provided
            return True
        if shutil.which(first) is None and not Path(first).exists():
            return False
        return True

    async def restart(self, service: str) -> RestartCommandResult:
        command = self._build_command(service)

        def _execute() -> RestartCommandResult:
            completed = subprocess.run(
                command,
                cwd=str(self._working_dir),
                env=self._env,
                capture_output=True,
                text=True,
                check=False,
            )
            return RestartCommandResult(
                service=service,
                command=tuple(command),
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                mode=self.mode,
            )

        return await asyncio.to_thread(_execute)


DEFAULT_SERVICE_DEPENDENCIES: Mapping[str, Sequence[str]] = {
    "queue_monitor": (),
    "ib_command": (),
    "account": (),
    "orders": ("account",),
    "market_data_feed": (),
    "market_data": ("market_data_feed",),
    "risk": ("orders",),
    "notification": (),
    "strategy": ("orders",),
    "ibbot": ("account", "orders"),
}


class ServiceRestartManager:
    """Determine restart order and delegate to the underlying controller."""

    def __init__(
        self,
        controller: DockerComposeController | LocalSupervisorController,
        *,
        dependencies: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        mapping = dependencies or DEFAULT_SERVICE_DEPENDENCIES
        self._dependencies: dict[str, tuple[str, ...]] = {
            name: tuple(deps) for name, deps in mapping.items()
        }
        self._controller = controller

    @property
    def mode(self) -> RestartMode:
        return self._controller.mode

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
    ) -> list[RestartCommandResult]:
        plan = self.resolve_plan(service, include_dependencies=include_dependencies)
        results: list[RestartCommandResult] = []
        for name in plan:
            result = await self._controller.restart(name)
            if result.returncode != 0:
                raise CommandExecutionError(name, result)
            results.append(result)
        return results


_manager: ServiceRestartManager | None = None


def _normalise_mode(mode: RestartMode | str | None) -> RestartMode | None:
    if mode is None:
        return None
    if isinstance(mode, RestartMode):
        return mode
    try:
        return RestartMode(mode)
    except ValueError as exc:  # pragma: no cover - defensive guard
        raise ValueError(f"Unsupported restart mode: {mode}") from exc


def _build_controller_for_mode(mode: RestartMode):
    if mode is RestartMode.DOCKER:
        controller = DockerComposeController()
        if not controller.is_available():
            raise FileNotFoundError("docker compose tooling is unavailable")
        return controller

    if mode is RestartMode.LOCAL:
        controller = LocalSupervisorController()
        if not controller.is_available():
            raise FileNotFoundError("local supervisor command is unavailable")
        return controller

    raise ValueError(f"Unsupported restart mode: {mode}")


def _autodetect_controller() -> ServiceRestartManager:
    try:
        controller = _build_controller_for_mode(RestartMode.DOCKER)
    except FileNotFoundError:
        controller = _build_controller_for_mode(RestartMode.LOCAL)
    return ServiceRestartManager(controller)


def get_service_restart_manager(
    mode: RestartMode | str | None = None,
) -> ServiceRestartManager:
    """Return the singleton restart manager used by the API layer."""

    global _manager
    requested_mode = _normalise_mode(mode)
    if requested_mode is None:
        env_mode = os.getenv("SERVICE_RESTART_MODE")
        requested_mode = _normalise_mode(env_mode)

    if requested_mode is None:
        if _manager is None:
            _manager = _autodetect_controller()
        return _manager

    if _manager is None or _manager.mode is not requested_mode:
        controller = _build_controller_for_mode(requested_mode)
        _manager = ServiceRestartManager(controller)
    return _manager


def set_service_restart_manager(
    manager: ServiceRestartManager | None,
) -> None:
    """Override the global restart manager (primarily for tests)."""

    global _manager
    _manager = manager

