import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.routes import service as service_module
from src.service_restart import RestartCommandResult, RestartMode


class MockRestartManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []
        self.mode = RestartMode.DOCKER

    async def restart(self, service: str, *, include_dependencies: bool = True) -> list[RestartCommandResult]:
        self.calls.append((service, include_dependencies))
        return [
            RestartCommandResult(
                service="connection_control",
                command=("docker", "compose", "restart", "connection_control"),
                returncode=0,
                stdout="connection ok",
                stderr="",
                mode=self.mode,
            ),
            RestartCommandResult(
                service="account",
                command=("docker", "compose", "restart", "account"),
                returncode=0,
                stdout="account ok",
                stderr="",
                mode=self.mode,
            ),
            RestartCommandResult(
                service="orders",
                command=("docker", "compose", "restart", "orders"),
                returncode=0,
                stdout="orders ok",
                stderr="",
                mode=self.mode,
            ),
            RestartCommandResult(
                service="ibbot",
                command=("docker", "compose", "restart", "ibbot"),
                returncode=0,
                stdout="bot ok",
                stderr="",
                mode=self.mode,
            ),
        ]


class LocalMockRestartManager(MockRestartManager):
    def __init__(self) -> None:
        super().__init__()
        self.mode = RestartMode.LOCAL

    async def restart(
        self, service: str, *, include_dependencies: bool = True
    ) -> list[RestartCommandResult]:
        self.calls.append((service, include_dependencies))
        services = ("connection_control", "account", "orders", "ibbot")
        results: list[RestartCommandResult] = []
        for name in services:
            results.append(
                RestartCommandResult(
                    service=name,
                    command=("python", "scripts/run_service.py", name),
                    returncode=0,
                    stdout=f"{name} local ok",
                    stderr="",
                    mode=self.mode,
                )
            )
        return results


@pytest.fixture()
def restart_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    app = FastAPI()
    app.include_router(service_module.router)

    async def allow_user(request: Request) -> str:
        request.state.is_read_only = False
        request.state.user_role = "admin"
        request.state.username = "tester"
        return "tester"

    manager = MockRestartManager()
    monkeypatch.setattr(service_module, "get_service_restart_manager", lambda mode=None: manager)
    app.dependency_overrides[service_module.get_current_user] = allow_user
    app.state._mock_restart_manager = manager  # type: ignore[attr-defined]
    return app


def test_restart_endpoint_invokes_manager(restart_app: FastAPI) -> None:
    manager: MockRestartManager = restart_app.state._mock_restart_manager  # type: ignore[attr-defined]
    with TestClient(restart_app) as client:
        response = client.post("/api/services/restart", json={"service": "ibbot"})

    assert response.status_code == 200
    assert manager.calls == [("ibbot", True)]
    payload = response.json()
    assert payload["restarted"] == ["connection_control", "account", "orders", "ibbot"]
    assert payload["logs"][0]["stdout"] == "connection ok"
    assert payload["mode"] == RestartMode.DOCKER.value
    assert payload["message"]


def test_restart_endpoint_blocks_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastAPI()
    app.include_router(service_module.router)

    async def read_only_user(request: Request) -> str:
        request.state.is_read_only = True
        request.state.user_role = "visitor"
        request.state.username = "visitor"
        return "visitor"

    monkeypatch.setattr(service_module, "get_service_restart_manager", lambda mode=None: MockRestartManager())
    app.dependency_overrides[service_module.get_current_user] = read_only_user

    with TestClient(app) as client:
        response = client.post("/api/services/restart", json={"service": "ibbot"})

    assert response.status_code == 403
    assert response.json()["detail"].lower().startswith("read-only")


def test_restart_local_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastAPI()
    app.include_router(service_module.router)

    async def allow_user(request: Request) -> str:
        request.state.is_read_only = False
        request.state.user_role = "admin"
        request.state.username = "tester"
        return "tester"

    manager = LocalMockRestartManager()
    monkeypatch.setattr(
        service_module,
        "get_service_restart_manager",
        lambda mode=None: manager,
    )
    app.dependency_overrides[service_module.get_current_user] = allow_user

    with TestClient(app) as client:
        response = client.post(
            "/api/services/restart/local",
            json={"service": "ibbot", "include_dependencies": True},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == RestartMode.LOCAL.value
    assert payload["restarted"] == ["connection_control", "account", "orders", "ibbot"]
    assert all(log.get("mode") == RestartMode.LOCAL.value for log in payload["logs"])


def test_frontend_controller_updates_status() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    script_path = (repo_root / "static/js/service_restart.js").as_posix()
    node_script = f"""
const fs = require('fs');
const vm = require('vm');
const code = fs.readFileSync({json.dumps(script_path)}, 'utf8');

function createClassList() {{
    const store = new Set();
    return {{
        add: (cls) => store.add(cls),
        remove: (cls) => store.delete(cls),
        contains: (cls) => store.has(cls),
        toArray: () => Array.from(store),
    }};
}}

const spinnerEl = {{ classList: createClassList() }};
const statusEl = {{ dataset: {{}}, textContent: '', classList: createClassList() }};
const selectEl = {{
    value: 'ibbot',
    classList: createClassList(),
    selectedOptions: [{{ value: 'ibbot', textContent: '主应用', dataset: {{ label: '主应用' }} }}],
}};
const buttonEl = {{
    disabled: false,
    classList: createClassList(),
    listeners: {{}},
    addEventListener: function(type, handler) {{ this.listeners[type] = handler; }},
    querySelector: function(sel) {{ if (sel === '.spinner-border') return spinnerEl; return null; }},
}};

const context = {{
    console: {{ log: () => {{}}, warn: () => {{}}, error: () => {{}} }},
    setTimeout,
    clearTimeout,
    window: {{}},
    document: {{}},
    appModules: {{}},
    fetchCalls: [],
}};
context.window = context;
context.globalThis = context;

vm.createContext(context);
vm.runInContext(code, context);

const controller = context.appModules.serviceRestart.createController({{
    button: buttonEl,
    select: selectEl,
    status: statusEl,
    spinner: spinnerEl,
    fetcher: async (request) => {{
        context.fetchCalls.push(request);
        return {{
            message: '所有服务重启完成',
            restarted: ['connection_control', 'account', 'orders', 'ibbot'],
            mode: 'docker',
            restart_endpoint: '/services/restart',
        }};
    }},
    runningMessage: '运行 {{service}}',
    successMessage: '完成 {{services}}',
    initialMessage: '准备就绪',
}});

async function main() {{
    await controller.restart();
    return {{
        state: controller.state,
        statusText: statusEl.textContent,
        statusState: statusEl.dataset.state,
        fetchCalls: context.fetchCalls,
        spinnerHidden: spinnerEl.classList.contains('d-none'),
        statusClasses: statusEl.classList.toArray(),
    }};
}}

main().then((result) => {{
    console.log(JSON.stringify(result));
}}).catch((err) => {{
    console.log(JSON.stringify({{ error: err.message, state: controller.state, statusText: statusEl.textContent }}));
    process.exit(1);
}});
"""

    completed = subprocess.run(
        ["node", "-e", node_script],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    payload: dict[str, Any] = json.loads(completed.stdout.strip() or "{}")
    assert payload["state"] == "success"
    assert payload["statusText"] == "所有服务重启完成"
    assert payload["statusState"] == "success"
    assert payload["fetchCalls"] == [
        {
            "endpoint": "/services/restart",
            "payload": {"service": "ibbot", "include_dependencies": True},
            "mode": "docker",
        }
    ]
    assert payload["spinnerHidden"] is True
