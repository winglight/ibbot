from typing import Any

import pytest

from core.container import CoreContainer


class Sentinel:
    def __init__(self, name: str) -> None:
        self.name = name


@pytest.mark.asyncio
async def test_core_container_lazy_dependencies():
    redis_calls = 0
    provider_calls = 0
    websocket_calls = 0
    connection_calls = 0
    dom_service_calls = 0
    dom_config_calls = 0
    dom_processor_calls = 0
    depth_calls = 0
    db_calls = 0
    data_layer_calls = 0

    async def redis_initializer(force: bool = False) -> Any:
        nonlocal redis_calls
        redis_calls += 1
        return f"redis-{force}"

    async def config_provider_factory() -> Any:
        nonlocal provider_calls
        provider_calls += 1
        return Sentinel("config")

    def websocket_factory() -> Any:
        nonlocal websocket_calls
        websocket_calls += 1
        return Sentinel("websocket")

    connection_instance = Sentinel("connection")
    dom_service_instance = Sentinel("dom-service")
    dom_config_instance = Sentinel("dom-config")
    dom_processor_instance = Sentinel("dom-processor")
    depth_instance = Sentinel("depth")
    db_manager_instance = Sentinel("db-manager")
    data_layer_instance = Sentinel("data-layer")

    def connection_factory() -> Any:
        nonlocal connection_calls
        connection_calls += 1
        return connection_instance

    def dom_service_factory() -> Any:
        nonlocal dom_service_calls
        dom_service_calls += 1
        return dom_service_instance

    def dom_config_factory() -> Any:
        nonlocal dom_config_calls
        dom_config_calls += 1
        return dom_config_instance

    def dom_processor_factory() -> Any:
        nonlocal dom_processor_calls
        dom_processor_calls += 1
        return dom_processor_instance

    def depth_factory() -> Any:
        nonlocal depth_calls
        depth_calls += 1
        return depth_instance

    def db_factory() -> Any:
        nonlocal db_calls
        db_calls += 1
        return db_manager_instance

    def data_layer_factory() -> Any:
        nonlocal data_layer_calls
        data_layer_calls += 1
        return data_layer_instance

    container = CoreContainer(
        redis_initializer=redis_initializer,
        config_provider_factory=config_provider_factory,
        connection_manager_factory=connection_factory,
        websocket_manager_factory=websocket_factory,
        dom_service_factory=dom_service_factory,
        dom_config_factory=dom_config_factory,
        dom_processor_factory=dom_processor_factory,
        depth_subscriber_factory=depth_factory,
        db_manager_factory=db_factory,
        data_source_manager_factory=data_layer_factory,
    )

    assert container.redis_client is None

    await container.ensure_redis()
    assert redis_calls == 1
    assert container.redis_client == "redis-False"

    # Cached result without force reuse existing client
    await container.ensure_redis()
    assert redis_calls == 1

    # Force refresh triggers a new call and updates cache
    await container.ensure_redis(force=True)
    assert redis_calls == 2
    assert container.redis_client == "redis-True"

    provider = await container.get_config_provider()
    assert provider_calls == 1
    assert provider is await container.get_config_provider()
    assert provider_calls == 1

    ws = container.websocket_manager
    assert websocket_calls == 1
    assert ws is container.websocket_manager
    assert websocket_calls == 1

    assert container.connection_manager is connection_instance
    assert connection_calls == 1
    assert container.dom_service is dom_service_instance
    assert dom_service_calls == 1
    assert container.dom_config is dom_config_instance
    assert dom_config_calls == 1
    assert container.dom_processor is dom_processor_instance
    assert dom_processor_calls == 1
    assert container.depth_subscriber is depth_instance
    assert depth_calls == 1
    assert container.get_db_manager() is db_manager_instance
    assert db_calls == 1
    assert container.get_data_source_manager() is data_layer_instance
    assert data_layer_calls == 1


def test_core_container_sync_initialisation():
    calls = 0

    async def redis_initializer(force: bool = False) -> Any:
        nonlocal calls
        calls += 1
        return "sync-redis"

    container = CoreContainer(redis_initializer=redis_initializer)
    result = container.ensure_redis_sync()
    assert result == "sync-redis"
    assert calls == 1

    # The synchronous path should reuse the cached value without reinitialising
    result = container.ensure_redis_sync()
    assert result == "sync-redis"
    assert calls == 1
