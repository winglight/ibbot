from __future__ import annotations

import asyncio

import pytest

import core
from core import container as core_container


class DummyConfigProvider:
    def __init__(self, redis_client=None) -> None:  # noqa: D401 - simple stub
        self.redis_client = redis_client
        self.started = False
        self.closed = False

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True


def test_initialise_sets_order_manager_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    dummy_redis = object()
    influx_config = {"mock": True}

    async def fake_init_redis():
        return dummy_redis

    def fake_configure_influx():
        return influx_config

    monkeypatch.setattr(core_container, "init_redis", fake_init_redis)
    monkeypatch.setattr(core_container, "configure_influx", fake_configure_influx)
    monkeypatch.setattr(core_container, "ConfigProvider", DummyConfigProvider)
    monkeypatch.setattr(core_container, "load_project_env", lambda: None)

    async def scenario() -> DummyConfigProvider:
        container = await core_container.initialise(force=True)

        assert container.redis_client is dummy_redis
        assert container.influx_config == influx_config
        assert isinstance(container.config_provider, DummyConfigProvider)
        assert container.config_provider.started is True
        assert hasattr(container, "order_manager")
        assert container.order_manager is None
        assert core_container.require_container() is container

        assert getattr(core, "order_manager") is None

        await core_container.shutdown()
        return container.config_provider

    provider = asyncio.run(scenario())

    assert provider.closed is True
    assert core_container.get_container() is None
