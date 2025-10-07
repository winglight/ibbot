"""Integration tests for DOM HTTP routes using the real FastAPI app."""

from __future__ import annotations

import asyncio
from http import HTTPStatus
import importlib
from typing import Any

import pytest

class DummyDomService:
    """Lightweight stand-in exposing the behaviour expected by the DOM routes."""

    def __init__(self) -> None:
        self.start_calls: list[tuple[str, int, bool]] = []
        self.start_stream_calls: list[tuple[str, int, bool]] = []
        self.stop_calls: list[str] = []
        self.active_symbols: set[str] = set()
        self.pending_symbols: set[str] = set()
        self.in_progress: set[str] = set()
        self.raise_on_request: bool = False
        self.start_result: bool = True
        self.stop_result: bool = True
        self.stop_delay: float = 0.0
        self.last_error: tuple[str | None, int] = ("Failed to start depth stream", 500)
        self.metrics_response: dict[str, Any] = {"long_signal": 0.1, "short_signal": 0.2}
        self.depth_response: dict[str, Any] = {"symbol": None, "bids": [], "asks": []}
        self.alerts: list[dict[str, Any]] = []
        self.history_response: dict[str, Any] = {"rows": [], "snapshots": []}
        self.stats_response: dict[str, Any] = {"symbol": "", "series": []}
        self.metrics_calls: list[str | None] = []
        self.depth_calls: list[str | None] = []
        self.history_calls: list[tuple[str, dict[str, Any]]] = []
        self.stats_calls: list[tuple[str, dict[str, Any]]] = []

    # ------------------------------------------------------------------
    def is_stream_active(self, symbol: str) -> bool:
        return symbol.upper() in self.active_symbols

    def is_start_in_progress(self, symbol: str) -> bool:
        return symbol.upper() in self.in_progress

    def request_stream_start(
        self, symbol: str, num_rows: int = 5, *, force_restart: bool = False
    ) -> asyncio.Future:
        if self.raise_on_request:
            raise RuntimeError("unable to schedule start")

        symbol_key = symbol.upper()
        self.start_calls.append((symbol_key, int(num_rows), bool(force_restart)))

        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()

        if symbol_key in self.pending_symbols:
            self.in_progress.add(symbol_key)
            return future

        if self.start_result:
            self.active_symbols.add(symbol_key)
        future.set_result(self.start_result)
        self.in_progress.discard(symbol_key)
        return future

    async def start_stream(
        self, symbol: str, num_rows: int = 5, *, force_restart: bool = False
    ) -> bool:
        symbol_key = symbol.upper()
        self.start_stream_calls.append((symbol_key, int(num_rows), bool(force_restart)))
        if self.start_result:
            self.active_symbols.add(symbol_key)
        else:
            self.active_symbols.discard(symbol_key)
        return self.start_result

    def get_last_start_error(self) -> tuple[str | None, int]:
        return self.last_error

    async def stop_stream(self, symbol: str) -> bool:
        symbol_key = symbol.upper()
        self.stop_calls.append(symbol_key)
        if self.stop_delay:
            await asyncio.sleep(self.stop_delay)
        if self.stop_result:
            self.active_symbols.discard(symbol_key)
        return self.stop_result

    def get_metrics(self, symbol: str | None = None) -> dict[str, Any]:
        self.metrics_calls.append(symbol)
        return self.metrics_response

    def get_last_depth(self, symbol: str | None = None) -> dict[str, Any]:
        self.depth_calls.append(symbol)
        payload = dict(self.depth_response)
        payload.setdefault("symbol", (symbol or "").upper())
        return payload

    def list_alerts(self) -> list[dict[str, Any]]:
        return list(self.alerts)

    def ack_alert(self, alert_id: int) -> bool:
        for idx, alert in enumerate(list(self.alerts)):
            if alert.get("id") == alert_id:
                self.alerts.pop(idx)
                return True
        return False

    def get_history(self, symbol: str, **kwargs: Any) -> dict[str, Any]:
        self.history_calls.append((symbol, kwargs))
        return dict(self.history_response)

    async def get_stats_async(self, symbol: str, **kwargs: Any) -> dict[str, Any]:
        self.stats_calls.append((symbol, kwargs))
        return dict(self.stats_response)


@pytest.fixture()
def dom_routes_module(app):  # noqa: PT004
    """Return the DOM routes module after the app has been initialised."""

    return importlib.import_module("src.routes.dom")


@pytest.fixture()
def dom_stub(monkeypatch: pytest.MonkeyPatch, dom_routes_module) -> DummyDomService:  # noqa: PT004
    stub = DummyDomService()
    monkeypatch.setattr(dom_routes_module, "dom_service", stub)
    return stub


def test_dom_start_triggers_service_request(client, admin_headers, dom_stub):
    response = client.post(
        "/api/dom/start",
        headers=admin_headers,
        json={"symbol": "MNQ", "num_rows": 7},
    )

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload == {"success": True, "pending": False, "already_running": False}
    assert dom_stub.start_calls == [("MNQ", 7, False)]


def test_dom_start_requires_symbol(client, admin_headers, dom_stub):
    response = client.post(
        "/api/dom/start",
        headers=admin_headers,
        json={"symbol": "   "},
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["detail"] == "Symbol is required"
    assert dom_stub.start_calls == []


def test_dom_start_rejects_read_only_user(client, visitor_headers, dom_stub):
    response = client.post(
        "/api/dom/start",
        headers=visitor_headers,
        json={"symbol": "ES"},
    )

    assert response.status_code == HTTPStatus.FORBIDDEN
    assert response.json()["detail"] == "Read-only access"
    assert dom_stub.start_calls == []


def test_dom_start_reports_existing_stream(client, admin_headers, dom_stub):
    dom_stub.active_symbols.add("MNQ")

    response = client.post(
        "/api/dom/start",
        headers=admin_headers,
        json={"symbol": "MNQ"},
    )

    assert response.status_code == HTTPStatus.OK
    data = response.json()
    assert data["success"] is True
    assert data["already_running"] is True
    assert data["pending"] is False


def test_dom_start_marks_pending_when_task_times_out(
    client, admin_headers, dom_stub, dom_routes_module, monkeypatch
):
    dom_stub.pending_symbols.add("MNQ")

    async def immediate_timeout(task: asyncio.Future, timeout: float):  # pragma: no cover - patched behaviour
        raise asyncio.TimeoutError

    monkeypatch.setattr(dom_routes_module.asyncio, "wait_for", immediate_timeout)

    response = client.post(
        "/api/dom/start",
        headers=admin_headers,
        json={"symbol": "MNQ"},
    )

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["pending"] is True
    assert payload.get("starting") is True
    assert dom_stub.is_start_in_progress("MNQ")


def test_dom_start_falls_back_to_blocking_call(client, admin_headers, dom_stub):
    dom_stub.raise_on_request = True

    response = client.post(
        "/api/dom/start",
        headers=admin_headers,
        json={"symbol": "MES", "force_restart": True},
    )

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload == {"success": True, "pending": False}
    assert dom_stub.start_stream_calls == [("MES", 5, True)]


def test_dom_start_propagates_failure(client, admin_headers, dom_stub):
    dom_stub.start_result = False
    dom_stub.last_error = ("Rate limited", 429)

    response = client.post(
        "/api/dom/start",
        headers=admin_headers,
        json={"symbol": "MNQ"},
    )

    assert response.status_code == 429
    assert response.json()["detail"] == "Rate limited"


def test_dom_stop_requires_symbol(client, admin_headers, dom_stub):
    response = client.post(
        "/api/dom/stop",
        headers=admin_headers,
        json={"symbol": ""},
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["detail"] == "Symbol is required"
    assert dom_stub.stop_calls == []


def test_dom_stop_returns_success(client, admin_headers, dom_stub):
    response = client.post(
        "/api/dom/stop",
        headers=admin_headers,
        json={"symbol": "MNQ"},
    )

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload == {"success": True, "pending": False}
    assert dom_stub.stop_calls == ["MNQ"]


def test_dom_stop_reports_pending_when_timeout(
    client, admin_headers, dom_stub, dom_routes_module, monkeypatch
):
    dom_stub.stop_delay = 0.05
    monkeypatch.setattr(dom_routes_module, "DOM_STOP_TIMEOUT", 0.01)

    response = client.post(
        "/api/dom/stop",
        headers=admin_headers,
        json={"symbol": "MES"},
    )

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["success"] is True
    assert payload["pending"] is True
    assert dom_stub.stop_calls == ["MES"]


def test_dom_data_endpoint_returns_service_payload(client, admin_headers, dom_stub):
    dom_stub.metrics_response = {"obi": 0.5, "ofi": -0.1}
    dom_stub.depth_response = {"symbol": "MNQ", "bids": [[4400.0, 2]], "asks": [[4400.25, 1]]}

    response = client.get(
        "/api/dom/data",
        headers=admin_headers,
        params={"symbol": "MNQ"},
    )

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["success"] is True
    assert payload["metrics"] == dom_stub.metrics_response
    assert payload["depth"] == dom_stub.depth_response
    assert dom_stub.metrics_calls == ["MNQ"]
    assert dom_stub.depth_calls == ["MNQ"]


def test_dom_alert_routes_list_and_acknowledge(client, admin_headers, dom_stub):
    dom_stub.alerts = [
        {"id": 1, "message": "First", "severity": "info"},
        {"id": 2, "message": "Second", "severity": "warning"},
    ]

    list_response = client.get("/api/alerts", headers=admin_headers)
    assert list_response.status_code == HTTPStatus.OK
    alerts_payload = list_response.json()
    assert alerts_payload["success"] is True
    assert alerts_payload["alerts"] == dom_stub.alerts

    ack_response = client.post("/api/alerts/1/ack", headers=admin_headers)
    assert ack_response.status_code == HTTPStatus.OK
    assert ack_response.json() == {"success": True}
    assert dom_stub.alerts == [{"id": 2, "message": "Second", "severity": "warning"}]


def test_dom_ack_alert_not_found(client, admin_headers, dom_stub):
    dom_stub.alerts = [{"id": 5, "message": "Existing"}]

    response = client.post("/api/alerts/4/ack", headers=admin_headers)
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["detail"] == "Alert not found"
    assert dom_stub.alerts == [{"id": 5, "message": "Existing"}]


def test_dom_history_forwards_parameters(client, admin_headers, dom_stub):
    dom_stub.history_response = {"rows": [{"ts": 1, "obi": 0.1}], "snapshots": []}

    response = client.get(
        "/api/dom/history/MNQ",
        headers=admin_headers,
        params={
            "limit": 0,
            "start": "2024-01-01T00:00:00Z",
            "end": "2024-01-01T01:00:00Z",
            "include_snapshots": "false",
        },
    )

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["success"] is True
    assert payload["rows"] == dom_stub.history_response["rows"]
    assert dom_stub.history_calls == [
        (
            "MNQ",
            {
                "limit": None,
                "start_time": "2024-01-01T00:00:00Z",
                "end_time": "2024-01-01T01:00:00Z",
                "include_snapshots": False,
            },
        )
    ]


def test_dom_stats_endpoint_awaits_service_call(client, admin_headers, dom_stub):
    dom_stub.stats_response = {"symbol": "MNQ", "series": [{"ts": 1, "obi": 0.2}]}

    response = client.get(
        "/api/dom/stats/MNQ",
        headers=admin_headers,
        params={
            "lookback_seconds": 120,
            "start": "2024-01-02T00:00:00Z",
            "end": "2024-01-02T00:30:00Z",
            "max_points": 10,
        },
    )

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["success"] is True
    assert payload["series"] == dom_stub.stats_response["series"]
    assert dom_stub.stats_calls == [
        (
            "MNQ",
            {
                "lookback_seconds": 120,
                "start_time": "2024-01-02T00:00:00Z",
                "end_time": "2024-01-02T00:30:00Z",
                "max_points": 10,
            },
        )
    ]
