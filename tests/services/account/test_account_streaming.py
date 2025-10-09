from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.account.main import app, _state
from src.ib.events import AccountUpdateEvent, PortfolioPositionEvent


class _StubSnapshotService:
    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def request_immediate_refresh(self, reason: str = "manual") -> None:
        return None

    async def refresh_account_info(self, *, reason: str = "manual") -> None:
        return None

    async def refresh_positions(self, *, reason: str = "manual", fast: bool = True) -> None:
        return None

    async def refresh_both(
        self, *, reason: str = "manual", fast_positions: bool = True
    ) -> None:
        return None


class _StubPublisher:
    def __init__(self) -> None:
        self.messages: list[tuple[str, Any]] = []
        self.signal = threading.Event()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _StubClient:
    def __init__(self) -> None:
        self.callback_queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.connected = False
        self.subscribe_calls: list[list[str]] = []
        self.unsubscribe_calls = 0
        self.disconnect_calls = 0

    async def connect(self, *, timeout: float | None = None) -> None:
        self.loop = asyncio.get_running_loop()
        self.connected = True

    def isConnected(self) -> bool:  # noqa: N802 - signature mirrors ibapi
        return self.connected

    async def disconnect(self) -> None:
        self.connected = False
        self.disconnect_calls += 1

    async def async_disconnect(self) -> None:
        await self.disconnect()

    async def wait_until_ready(self, timeout: float | None = None) -> None:
        return None

    async def subscribe_account_updates(self, accounts: list[str]) -> None:
        self.subscribe_calls.append(list(accounts))

    async def unsubscribe_account_updates(self) -> None:
        self.unsubscribe_calls += 1

    async def fetch_account_summary(self, tags: str, *, timeout: float | None = None) -> dict[str, Any]:
        return {
            "request_id": 42,
            "accounts": {
                "DU1": {
                    "NetLiquidation": Decimal("1000.00"),
                }
            },
            "currencies": {"DU1": {"NetLiquidation": "USD"}},
            "received_at": datetime.now(timezone.utc),
        }

    async def fetch_positions(
        self, *, timeout: float | None = None
    ) -> list[PortfolioPositionEvent]:
        return [
            PortfolioPositionEvent(
                account="DU1",
                contract_id=1,
                symbol="ES",
                security_type="FUT",
                position=Decimal("1"),
                average_cost=Decimal("4200.50"),
                market_price=Decimal("4300.25"),
                market_value=Decimal("4300.25"),
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("100"),
                currency="USD",
            ),
            PortfolioPositionEvent(
                account="DU2",
                contract_id=2,
                symbol="NQ",
                security_type="FUT",
                position=Decimal("2"),
                average_cost=Decimal("15000"),
                market_price=Decimal("15100"),
                market_value=Decimal("30200"),
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("200"),
                currency="USD",
            ),
        ]


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    _state.redis = None
    _state.ib_client = None
    _state.publisher = None
    _state.event_task = None
    _state.shutdown_event = None
    _state.subscription_lock = None
    _state.subscription_accounts = set()
    _state.subscription_tags = set()
    _state.subscription_count = 0


def test_account_subscription_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_client = _StubClient()
    stub_publisher = _StubPublisher()

    async def _fake_get_redis_client(*_: Any, **__: Any) -> Any:
        return object()

    async def _fake_publish_account_event(publisher: _StubPublisher, event: Any) -> None:
        publisher.messages.append(("account_update", event))
        publisher.signal.set()

    async def _fake_publish_portfolio_event(publisher: _StubPublisher, event: Any) -> None:
        publisher.messages.append(("portfolio", event))
        publisher.signal.set()

    async def _fake_update_service_health(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr("services.account.main.get_redis_client", _fake_get_redis_client)
    monkeypatch.setattr("services.account.main.update_service_health", _fake_update_service_health)
    monkeypatch.setattr("services.account.main.AccountSnapshotService", _StubSnapshotService)
    monkeypatch.setattr("services.account.main.ServiceIBClient", lambda: stub_client)
    monkeypatch.setattr("services.account.main.RedisPublisher", lambda: stub_publisher)
    monkeypatch.setattr(
        "services.account.main.publish_account_event", _fake_publish_account_event
    )
    monkeypatch.setattr(
        "services.account.main.publish_portfolio_event", _fake_publish_portfolio_event
    )

    with TestClient(app) as client:
        response = client.post(
            "/subscriptions/account",
            json={"accounts": ["DU1"], "tags": ["NetLiquidation"]},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["subscription_count"] == 1
        assert payload["accounts"] == ["DU1"]
        assert payload["summary"]["DU1"]["NetLiquidation"] == "1000.00"

        assert stub_client.loop is not None
        update_event = AccountUpdateEvent(
            account="DU1",
            key="NetLiquidation",
            value=Decimal("1000.00"),
            currency="USD",
        )
        future = asyncio.run_coroutine_threadsafe(
            stub_client.callback_queue.put(("account_update", update_event)),
            stub_client.loop,
        )
        future.result(timeout=1)
        assert stub_publisher.signal.wait(timeout=1)
        assert stub_publisher.messages
        channel, event = stub_publisher.messages[0]
        assert channel == "account_update"
        assert isinstance(event, AccountUpdateEvent)

        summary_response = client.get(
            "/accounts/DU1/summary", params={"tags": "NetLiquidation"}
        )
        assert summary_response.status_code == 200
        summary_payload = summary_response.json()
        assert summary_payload["summary"]["NetLiquidation"] == "1000.00"

        positions_response = client.get("/accounts/DU1/positions")
        assert positions_response.status_code == 200
        positions_payload = positions_response.json()
        assert positions_payload["count"] == 1
        assert positions_payload["positions"][0]["symbol"] == "ES"

        delete_response = client.delete("/subscriptions/account")
        assert delete_response.status_code == 200
        delete_payload = delete_response.json()
        assert delete_payload["remaining_subscribers"] == 0
        assert stub_client.unsubscribe_calls == 1
        assert stub_client.disconnect_calls == 1

