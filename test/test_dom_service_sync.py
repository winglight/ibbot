import asyncio

import pytest

from src.dom_service import DOMService
from src.data_layer.config import DataLayerMode


@pytest.mark.asyncio
async def test_dom_service_start_from_running_loop(monkeypatch):
    """DOMService.start should work when invoked within an active event loop."""

    monkeypatch.setattr("src.dom_service.get_automation_defaults", lambda: None)
    monkeypatch.setattr("src.dom_service.get_data_layer_mode", lambda: DataLayerMode.UNIFIED)

    service = DOMService()
    monkeypatch.setattr(service, "_is_data_layer_enabled", lambda: True)

    call_order = []

    async def fake_start(symbol: str, num_rows: int = 5, *, force_restart: bool = False):
        call_order.append((symbol, num_rows, force_restart))
        # Ensure the coroutine runs inside a loop created by asyncio.run
        loop = asyncio.get_running_loop()
        assert loop.is_running()
        return True

    monkeypatch.setattr(service, "_start_stream_unified", fake_start)

    assert service.start("MNQ") is True
    assert call_order == [("MNQ", 5, False)]
