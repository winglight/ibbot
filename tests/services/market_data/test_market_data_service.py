from __future__ import annotations

import asyncio
import threading
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.market_data.main import app, _state
from src.ib.events import DOMLevelEvent, MarketTickEvent


class _StubPublisher:
    def __init__(self) -> None:
        self.messages: list[tuple[str, Any]] = []
        self.signal = threading.Event()
        self.closed = False

    async def publish(self, channel: str, payload: Any) -> None:
        self.messages.append((channel, payload))
        self.signal.set()

    async def close(self) -> None:
        self.closed = True


class _StubClient:
    def __init__(self) -> None:
        self.callback_queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.quote_subscriptions: list[tuple[Any, list[int], bool]] = []
        self.dom_subscriptions: list[tuple[Any, int, str | None]] = []
        self.tick_subscriptions: list[tuple[Any, str]] = []
        self.bar_subscriptions: list[tuple[Any, str, str]] = []
        self.unsubscribe_calls: list[str] = []
        self.market_data_type_requests: list[int] = []
        self.historical_requests: list[dict[str, Any]] = []

    async def connect(self, *, timeout: float | None = None) -> None:
        self.loop = asyncio.get_running_loop()

    async def wait_until_ready(self, timeout: float | None = None) -> None:
        return None

    def reqMarketDataType(self, value: int) -> None:  # noqa: N802 - IB API signature
        self.market_data_type_requests.append(value)

    async def subscribe_quotes(
        self, contract: Any, *, snapshot: bool, tick_types: list[int]
    ) -> str:
        self.quote_subscriptions.append((contract, list(tick_types), snapshot))
        return f"sub-quotes-{len(self.quote_subscriptions)}"

    async def unsubscribe_quotes(self, subscription_id: str) -> bool:
        self.unsubscribe_calls.append(subscription_id)
        return True

    async def subscribe_dom(
        self, contract: Any, *, depth: int, exchange: str | None
    ) -> str:
        self.dom_subscriptions.append((contract, depth, exchange))
        return f"sub-dom-{len(self.dom_subscriptions)}"

    async def unsubscribe_dom(self, subscription_id: str) -> bool:
        self.unsubscribe_calls.append(subscription_id)
        return True

    async def subscribe_tick_by_tick(
        self, contract: Any, *, tick_type: str
    ) -> str:
        self.tick_subscriptions.append((contract, tick_type))
        return f"sub-tick-{len(self.tick_subscriptions)}"

    async def unsubscribe_tick_by_tick(self, subscription_id: str) -> bool:
        self.unsubscribe_calls.append(subscription_id)
        return True

    async def subscribe_real_time_bars(
        self, contract: Any, *, bar_size: str, what_to_show: str
    ) -> str:
        self.bar_subscriptions.append((contract, bar_size, what_to_show))
        return f"sub-bars-{len(self.bar_subscriptions)}"

    async def unsubscribe_real_time_bars(self, subscription_id: str) -> bool:
        self.unsubscribe_calls.append(subscription_id)
        return True

    async def fetch_historical(
        self,
        contract: Any,
        *,
        duration: str,
        bar_size: str,
        what_to_show: str,
        keep_up_to_date: bool,
    ) -> list[dict[str, Any]]:
        request = {
            "contract": contract,
            "duration": duration,
            "bar_size": bar_size,
            "what_to_show": what_to_show,
            "keep_up_to_date": keep_up_to_date,
        }
        self.historical_requests.append(request)
        return [
            {
                "time": "20240101 12:00:00",
                "open": 100.0,
                "high": 101.0,
                "low": 99.5,
                "close": 100.5,
                "volume": 10,
                "wap": 100.8,
                "count": 4,
            }
        ]


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    _state.redis = None
    _state.publisher = None
    _state.ib_client = None
    _state.event_task = None
    _state.shutdown_event = None
    _state.subscription_lock = None
    _state.pending_subscriptions = 0
    _state.quote_subscriptions.clear()
    _state.dom_subscriptions.clear()
    _state.tick_by_tick_subscriptions.clear()
    _state.real_time_bar_subscriptions.clear()
    _state.contract_symbols.clear()
    _state.last_error = None
    _state.max_subscriptions = 50
    _state.delayed_fallback_enabled = True
    _state.using_delayed_data = False
    _state.current_market_data_type = None


def test_quote_subscription_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_client = _StubClient()
    stub_publisher = _StubPublisher()
    canonical_messages: list[str] = []

    async def _fake_get_redis_client(*_: Any, **__: Any) -> Any:
        return object()

    async def _fake_publish_market_tick_event(
        publisher: _StubPublisher, event: MarketTickEvent
    ) -> None:
        canonical_messages.append(event.tick_type)

    async def _fake_publish_dom_event(
        publisher: _StubPublisher, event: DOMLevelEvent
    ) -> None:
        canonical_messages.append(event.operation)

    async def _fake_publish_tick_by_tick_event(*_: Any, **__: Any) -> None:
        return None

    async def _fake_publish_real_time_bar_event(*_: Any, **__: Any) -> None:
        return None

    async def _fake_publish_error_event(*_: Any, **__: Any) -> None:
        return None

    async def _fake_update_service_health(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr("services.market_data.main.get_redis_client", _fake_get_redis_client)
    monkeypatch.setattr("services.market_data.main.update_service_health", _fake_update_service_health)
    monkeypatch.setattr("services.market_data.main.ServiceIBClient", lambda: stub_client)
    monkeypatch.setattr("services.market_data.main.RedisPublisher", lambda redis=None: stub_publisher)
    monkeypatch.setattr(
        "services.market_data.main.publish_market_tick_event",
        _fake_publish_market_tick_event,
    )
    monkeypatch.setattr("services.market_data.main.publish_dom_event", _fake_publish_dom_event)
    monkeypatch.setattr(
        "services.market_data.main.publish_tick_by_tick_event",
        _fake_publish_tick_by_tick_event,
    )
    monkeypatch.setattr(
        "services.market_data.main.publish_real_time_bar_event",
        _fake_publish_real_time_bar_event,
    )
    monkeypatch.setattr(
        "services.market_data.main.publish_error_event",
        _fake_publish_error_event,
    )

    with TestClient(app) as client:
        response = client.post(
            "/subscriptions/quotes",
            json={
                "contract": {"conId": 123, "symbol": "ES", "exchange": "GLOBEX"},
                "tickTypes": [0, 1],
                "snapshot": False,
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["subscription_count"] == 1
        assert payload["symbol"] == "ES"
        assert stub_client.market_data_type_requests == [1]

        event = MarketTickEvent(
            contract_id=123,
            tick_type="LAST",
            value=Decimal("100.25"),
        )
        future = asyncio.run_coroutine_threadsafe(
            stub_client.callback_queue.put(("market_tick", event)),
            stub_client.loop,
        )
        future.result(timeout=1)
        assert stub_publisher.signal.wait(timeout=1)
        channel, message = stub_publisher.messages[0]
        assert channel == "md:quotes:ES"
        assert message["symbol"] == "ES"
        assert canonical_messages == ["LAST"]

        delete_response = client.delete(
            f"/subscriptions/quotes/{payload['subscription_id']}"
        )
        assert delete_response.status_code == 200
        assert delete_response.json()["subscription_count"] == 0
        assert payload["subscription_id"] in stub_client.unsubscribe_calls


def test_subscription_limit_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_client = _StubClient()
    stub_publisher = _StubPublisher()

    monkeypatch.setenv("MARKET_DATA_MAX_SUBSCRIPTIONS", "1")

    async def _fake_get_redis_client(*_: Any, **__: Any) -> Any:
        return object()

    async def _noop(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr("services.market_data.main.get_redis_client", _fake_get_redis_client)
    monkeypatch.setattr("services.market_data.main.update_service_health", _noop)
    monkeypatch.setattr("services.market_data.main.ServiceIBClient", lambda: stub_client)
    monkeypatch.setattr("services.market_data.main.RedisPublisher", lambda redis=None: stub_publisher)
    monkeypatch.setattr("services.market_data.main.publish_market_tick_event", _noop)
    monkeypatch.setattr("services.market_data.main.publish_dom_event", _noop)
    monkeypatch.setattr("services.market_data.main.publish_tick_by_tick_event", _noop)
    monkeypatch.setattr("services.market_data.main.publish_real_time_bar_event", _noop)
    monkeypatch.setattr("services.market_data.main.publish_error_event", _noop)

    with TestClient(app) as client:
        first = client.post(
            "/subscriptions/quotes",
            json={"contract": {"conId": 1, "symbol": "ES"}},
        )
        assert first.status_code == 200

        second = client.post(
            "/subscriptions/quotes",
            json={"contract": {"conId": 2, "symbol": "NQ"}},
        )
        assert second.status_code == 429


def test_historical_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_client = _StubClient()
    stub_publisher = _StubPublisher()

    async def _fake_get_redis_client(*_: Any, **__: Any) -> Any:
        return object()

    async def _noop(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr("services.market_data.main.get_redis_client", _fake_get_redis_client)
    monkeypatch.setattr("services.market_data.main.update_service_health", _noop)
    monkeypatch.setattr("services.market_data.main.ServiceIBClient", lambda: stub_client)
    monkeypatch.setattr("services.market_data.main.RedisPublisher", lambda redis=None: stub_publisher)
    monkeypatch.setattr("services.market_data.main.publish_market_tick_event", _noop)
    monkeypatch.setattr("services.market_data.main.publish_dom_event", _noop)
    monkeypatch.setattr("services.market_data.main.publish_tick_by_tick_event", _noop)
    monkeypatch.setattr("services.market_data.main.publish_real_time_bar_event", _noop)
    monkeypatch.setattr("services.market_data.main.publish_error_event", _noop)

    with TestClient(app) as client:
        response = client.get(
            "/historical",
            params={
                "conId": 77,
                "duration": "1 D",
                "barSize": "1 min",
                "whatToShow": "TRADES",
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["contract_id"] == 77
        assert payload["count"] == 1
        assert stub_client.historical_requests


def test_reload_configuration_updates_market_data_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_client = _StubClient()
    stub_publisher = _StubPublisher()

    async def _fake_get_redis_client(*_: Any, **__: Any) -> Any:
        return object()

    async def _noop(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setenv("MARKET_DATA_TYPE", "live")
    monkeypatch.setattr("services.market_data.main.get_redis_client", _fake_get_redis_client)
    monkeypatch.setattr("services.market_data.main.update_service_health", _noop)
    monkeypatch.setattr("services.market_data.main.ServiceIBClient", lambda: stub_client)
    monkeypatch.setattr("services.market_data.main.RedisPublisher", lambda redis=None: stub_publisher)
    monkeypatch.setattr("services.market_data.main.publish_market_tick_event", _noop)
    monkeypatch.setattr("services.market_data.main.publish_dom_event", _noop)
    monkeypatch.setattr("services.market_data.main.publish_tick_by_tick_event", _noop)
    monkeypatch.setattr("services.market_data.main.publish_real_time_bar_event", _noop)
    monkeypatch.setattr("services.market_data.main.publish_error_event", _noop)

    with TestClient(app) as client:
        response = client.post(
            "/subscriptions/quotes",
            json={"contract": {"conId": 10, "symbol": "ES"}},
        )
        assert response.status_code == 200
        assert stub_client.market_data_type_requests == [1]

        monkeypatch.setenv("MARKET_DATA_TYPE", "delayed")
        reload_response = client.post("/config/reload")
        assert reload_response.status_code == 200
        assert stub_client.market_data_type_requests[-1] == 3
        assert reload_response.json()["market_data_type"] == 3

