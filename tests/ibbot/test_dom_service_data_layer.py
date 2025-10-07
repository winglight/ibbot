import contextlib
from asyncio import CancelledError
from typing import Any, Dict, List, Tuple

import pytest

import src.dom_service as dom_service_module
from src.data_layer import DataSubscriptionToken
from src.data_layer.event_bus import BusSubscriptionToken, EventBus
from src.dom_dependencies import DOMConfig


class _DummyProcessor:
    def __init__(self, window: int, max_levels: int, parameter_tuner: Any | None = None) -> None:
        self.window = window
        self.max_levels = max_levels
        self.parameter_tuner = parameter_tuner
        self.last_metrics: Dict[str, Any] = {}
        self.last_signals: Dict[str, Any] = {}
        self.last_events: List[Any] = []

    def on_depth_update(self, snapshot: Dict[str, Any]) -> None:  # pragma: no cover - helper stub
        return None


class _DummyConnectionManager:
    def __init__(self) -> None:
        self._last_error: str | None = None

    def is_connected(self) -> bool:
        return True

    def get_last_connect_error(self) -> str | None:
        return self._last_error


class _FakeManager:
    def __init__(self) -> None:
        self.event_bus = EventBus()
        self.requests: List[Any] = []
        self.tokens: Dict[str, DataSubscriptionToken] = {}
        self.unsubscribed: List[DataSubscriptionToken] = []

    async def subscribe(self, request):  # pragma: no cover - exercised in tests
        token = DataSubscriptionToken(token=f"tok-{len(self.requests) + 1}", channel=request.channel)
        self.requests.append(request)
        self.tokens[token.token] = token
        return token

    async def unsubscribe(self, token):  # pragma: no cover - exercised in tests
        self.unsubscribed.append(token)
        self.tokens.pop(token.token, None)


@pytest.fixture()
def dom_service_setup(monkeypatch: pytest.MonkeyPatch) -> Tuple[dom_service_module.DOMService, _FakeManager, List[BusSubscriptionToken]]:
    monkeypatch.setattr(dom_service_module.atexit, "register", lambda *args, **kwargs: None)
    monkeypatch.setattr(dom_service_module, "load_dom_config", lambda: None)
    monkeypatch.setattr(dom_service_module, "save_dom_config", lambda payload: None)

    dom_config = DOMConfig()
    monkeypatch.setattr(dom_service_module, "dom_config", dom_config)
    dummy_processor = _DummyProcessor(window=dom_config.window, max_levels=dom_config.max_levels)
    monkeypatch.setattr(dom_service_module, "DOMProcessor", _DummyProcessor)
    monkeypatch.setattr(dom_service_module, "get_dom_processor", lambda: dummy_processor)
    monkeypatch.setattr(dom_service_module, "set_dom_processor", lambda _: None)

    manager = _FakeManager()
    unsubscribed_tokens: List[BusSubscriptionToken] = []
    original_unsubscribe = manager.event_bus.unsubscribe

    def _capture_unsubscribe(token: BusSubscriptionToken) -> None:
        unsubscribed_tokens.append(token)
        original_unsubscribe(token)

    monkeypatch.setattr(manager.event_bus, "unsubscribe", _capture_unsubscribe)

    monkeypatch.setattr(dom_service_module, "connection_manager", _DummyConnectionManager())
    monkeypatch.setattr(dom_service_module, "get_data_source_manager", lambda: manager)

    service = dom_service_module.DOMService()
    return service, manager, unsubscribed_tokens


@pytest.mark.asyncio
async def test_dom_service_uses_data_layer_manager(dom_service_setup):
    service, manager, unsubscribed_tokens = dom_service_setup

    assert not service.is_stream_active("HSI")

    started = await service.start_stream("HSI", num_rows=8)

    assert started is True
    assert manager.requests, "Data layer manager did not receive subscribe request"
    request = manager.requests[-1]
    assert request.channel == "dom.depth"
    assert request.symbol == "HSI"
    assert request.options["num_rows"] == 8
    assert "HSI" in service._data_layer_tokens  # noqa: SLF001 - internal state verification for test
    assert service.is_stream_active("HSI")
    assert service._data_layer_subscription is not None  # noqa: SLF001

    stopped = await service.stop_stream("HSI")

    assert stopped is True
    assert manager.unsubscribed, "Data layer manager did not receive unsubscribe request"
    assert unsubscribed_tokens, "Event bus subscription was not released"
    assert "HSI" not in service._data_layer_tokens  # noqa: SLF001
    assert service._data_layer_subscription is None  # noqa: SLF001
    assert not service.is_stream_active("HSI")

    service._shutdown_event.set()  # noqa: SLF001 - ensure cleanup task stops
    cleanup_task = service._cleanup_task  # noqa: SLF001
    if cleanup_task and not cleanup_task.done():
        cleanup_task.cancel()
        with contextlib.suppress(CancelledError):
            await cleanup_task
