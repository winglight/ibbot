from __future__ import annotations

import asyncio

import pytest

from src.connection_manager import ConnectionManager


class _DummyController:
    """Minimal stand-in used to assert controller wiring."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self.constructed_with = loop

    def is_connected(self) -> bool:  # pragma: no cover - helper parity
        return True


@pytest.mark.asyncio
async def test_controller_is_default_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure the connection manager prefers the IBAPI controller."""

    monkeypatch.delenv("IBKR_ENABLE_IB_INSYNC", raising=False)
    monkeypatch.setattr("src.connection_manager.IB", None, raising=False)
    monkeypatch.setattr("src.connection_manager._IB_IMPORT_IS_STUB", False, raising=False)
    monkeypatch.setattr("src.connection_manager.IBAPIController", _DummyController, raising=False)

    manager = ConnectionManager()

    assert not manager._should_use_legacy_ib_client()

    loop = asyncio.new_event_loop()
    try:
        controller = manager._ensure_ibapi_controller(loop)
        assert isinstance(controller, _DummyController)
        assert controller.constructed_with is loop
        assert manager._ibapi_controller is controller

        same_controller = manager._ensure_ibapi_controller(loop)
        assert same_controller is controller

        other_loop = asyncio.new_event_loop()
        try:
            next_controller = manager._ensure_ibapi_controller(other_loop)
            assert next_controller is not controller
            assert isinstance(next_controller, _DummyController)
            assert next_controller.constructed_with is other_loop
        finally:
            other_loop.close()
    finally:
        loop.close()


def test_legacy_opt_in_requires_real_ib(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opting into ib_insync should be ignored when the package is missing."""

    monkeypatch.setenv("IBKR_ENABLE_IB_INSYNC", "1")
    monkeypatch.setattr("src.connection_manager.IB", None, raising=False)
    monkeypatch.setattr("src.connection_manager._IB_IMPORT_IS_STUB", False, raising=False)

    manager = ConnectionManager()

    assert not manager._allow_legacy_ib
    assert not manager._should_use_legacy_ib_client()
