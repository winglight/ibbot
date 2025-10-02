import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.routes import connection as connection_module


@pytest.fixture()
def connection_app(monkeypatch: pytest.MonkeyPatch):
    app = FastAPI()
    app.include_router(connection_module.router)

    async def allow_user(request: Request) -> str:
        request.state.is_read_only = False
        request.state.user_role = "admin"
        request.state.username = "tester"
        return "tester"

    app.dependency_overrides[connection_module.get_current_user] = allow_user

    async def noop_audit(*args, **kwargs):
        return None

    monkeypatch.setattr(connection_module, "audit_log_success", noop_audit)
    monkeypatch.setattr(connection_module, "audit_log_failure", noop_audit)

    class StubContainer:
        redis_client = None
        config_provider = None

    container = StubContainer()
    monkeypatch.setattr(connection_module.core, "get_container", lambda: container)

    return app, container


def test_get_connection_config_prefers_provider(connection_app, monkeypatch: pytest.MonkeyPatch) -> None:
    app, container = connection_app

    class Provider:
        async def get_module(self, module: str, *, use_cache: bool = True):
            assert module == "ib-connection"
            return {
                "host": "redis-host",
                "port": "4010",
                "client_id": "12",
                "paper_trading": "false",
            }

    container.config_provider = Provider()

    class StubClient:
        def __init__(self) -> None:
            self.fetch_calls: int = 0

        async def fetch_config(self):  # pragma: no cover - not expected to be used
            self.fetch_calls += 1
            return {}

    stub_client = StubClient()
    monkeypatch.setattr(connection_module, "_get_connection_control_client", lambda: stub_client)

    with TestClient(app) as client:
        response = client.get("/api/connection_config")

    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "host": "redis-host",
        "port": 4010,
        "client_id": 12,
        "paper_trading": False,
    }
    assert stub_client.fetch_calls == 0


def test_get_connection_config_falls_back_to_env(connection_app, monkeypatch: pytest.MonkeyPatch) -> None:
    app, container = connection_app
    container.config_provider = None

    class FailingClient:
        async def fetch_config(self):
            raise connection_module.ServiceClientError("connection_control", "offline")

    monkeypatch.setattr(connection_module, "_get_connection_control_client", lambda: FailingClient())

    monkeypatch.setenv("IBKR_HOST", "env-host")
    monkeypatch.setenv("IBKR_PORT", "5000")
    monkeypatch.setenv("IBKR_CLIENT_ID", "42")
    monkeypatch.setenv("IBKR_PAPER_TRADING", "true")

    with TestClient(app) as client:
        response = client.get("/api/connection_config")

    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "host": "env-host",
        "port": 5000,
        "client_id": 42,
        "paper_trading": True,
    }


def test_connect_dispatches_service_command(connection_app, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = connection_app

    class StubClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def connect(self, payload):
            self.calls.append(dict(payload))
            return {"status": "queued", "command_id": "abc123"}

        async def fetch_config(self):  # pragma: no cover - not used here
            return {}

    stub_client = StubClient()
    monkeypatch.setattr(connection_module, "_get_connection_control_client", lambda: stub_client)

    payload = {
        "host": "1.2.3.4",
        "port": 4001,
        "client_id": 7,
        "paper_trading": False,
    }

    with TestClient(app) as client:
        response = client.post("/api/connect", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "success": True,
        "status": "dispatched",
        "service": "connection_control",
        "result": {"status": "queued", "command_id": "abc123"},
    }

    assert stub_client.calls == [payload]


def test_connect_returns_502_when_service_fails(connection_app, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = connection_app

    class StubClient:
        async def connect(self, payload):  # pragma: no cover - exercised in test
            raise connection_module.ServiceClientError("connection_control", "offline")

    monkeypatch.setattr(connection_module, "_get_connection_control_client", lambda: StubClient())

    with TestClient(app) as client:
        response = client.post(
            "/api/connect",
            json={
                "host": "localhost",
                "port": 4001,
                "client_id": 1,
                "paper_trading": True,
            },
        )

    assert response.status_code == 502
    assert response.json()["detail"] == "Failed to dispatch connect command"


def test_disconnect_enqueues_command(connection_app, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = connection_app

    enqueued: list[dict[str, object]] = []

    async def fake_enqueue(payload):
        enqueued.append(dict(payload))

    monkeypatch.setattr(connection_module, "_enqueue_command", fake_enqueue)

    with TestClient(app) as client:
        response = client.post("/api/disconnect")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "queued"
    assert enqueued and enqueued[0]["method"] == "disconnect"


def test_connection_status_returns_health_payload(connection_app, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = connection_app

    async def fake_health():
        return {
            "connected": True,
            "account_type": "Paper",
            "last_seen": "2024-01-01T00:00:00+00:00",
            "queue_depth": 1,
            "last_error": None,
            "error_code": 10147,
            "stale": False,
            "source": "redis",
            "ib_connected": True,
            "ib_connection_error": "",
        }

    monkeypatch.setattr(connection_module, "_load_ib_command_health", fake_health)

    with TestClient(app) as client:
        response = client.get("/api/connection_status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["connected"] is True
    assert payload["account_type"] == "Paper"
    assert payload["stale"] is False
    assert payload["error_code"] == 10147


def test_connection_status_marks_disconnected_gateways(connection_app, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = connection_app

    async def fake_health():
        return {
            "connected": False,
            "account_type": "Paper",
            "last_seen": "2024-01-01T00:00:00+00:00",
            "queue_depth": 3,
            "last_error": "gateway down",
            "error_code": None,
            "stale": False,
            "source": "redis",
            "ib_connected": False,
            "ib_connection_error": "gateway down",
        }

    monkeypatch.setattr(connection_module, "_load_ib_command_health", fake_health)

    with TestClient(app) as client:
        response = client.get("/api/connection_status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["connected"] is False
    assert payload["last_error"] == "gateway down"
    assert payload["stale"] is False


def test_connection_status_returns_default_when_unavailable(connection_app, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = connection_app

    async def missing_health():
        return None

    monkeypatch.setattr(connection_module, "_load_ib_command_health", missing_health)
    monkeypatch.setenv("IBKR_PAPER_TRADING", "false")

    with TestClient(app) as client:
        response = client.get("/api/connection_status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["connected"] is False
    assert payload["stale"] is True
    assert payload["account_type"] == "Live"
    assert payload["last_error"] == "ib-command health unavailable"
    assert payload["error_code"] is None
