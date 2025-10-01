import asyncio
from typing import Any

import pytest

pytestmark = pytest.mark.anyio("asyncio")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


from services.market_data.runtime import MarketDataRuntime
from src.configuration.provider import MarketDataServiceConfig


class StubProvider:
    def __init__(self, config: MarketDataServiceConfig) -> None:
        self._config = config
        self.started = False
        self.stopped = False
        self._callbacks: list[Any] = []

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def get_config(self) -> MarketDataServiceConfig:
        return self._config

    def register_callback(self, callback: Any) -> None:  # pragma: no cover - simple stub
        self._callbacks.append(callback)

    def unregister_callback(self, callback: Any) -> None:  # pragma: no cover - simple stub
        if callback in self._callbacks:
            self._callbacks.remove(callback)


class StubListener:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def start(self) -> None:
        self.started.set()

    async def stop(self) -> None:
        self.stopped.set()


@pytest.fixture
async def provider() -> StubProvider:
    config = MarketDataServiceConfig(
        symbols=["BTCUSDT"],
        stream_length=100,
        indicator_period=5,
        service_name="market-data-test",
        stream_key="stream:test",
        metrics_period=0.01,
    )
    stub = StubProvider(config)
    yield stub
    if not stub.stopped:
        await stub.stop()


async def test_startup_initialises_dependencies(provider: StubProvider) -> None:
    redis_instances: list[object] = []

    async def redis_factory() -> object:
        instance = object()
        redis_instances.append(instance)
        return instance

    listener = StubListener()

    def listener_factory(config: MarketDataServiceConfig, runtime: MarketDataRuntime) -> StubListener:
        assert runtime is not None
        return listener

    metrics_started = asyncio.Event()
    metrics_cancelled = asyncio.Event()

    async def metrics_factory(runtime: MarketDataRuntime) -> None:
        metrics_started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            metrics_cancelled.set()
            raise

    async def provider_factory() -> StubProvider:
        return provider

    runtime = MarketDataRuntime(
        config_provider_factory=provider_factory,
        redis_factory=redis_factory,
        listener_factory=listener_factory,
        metrics_factory=metrics_factory,
    )

    await runtime.startup()

    await asyncio.wait_for(metrics_started.wait(), timeout=0.1)

    assert provider.started is True
    assert runtime.redis in redis_instances
    assert listener.started.is_set()
    assert runtime.listener is listener
    assert runtime.metrics_task is not None
    assert runtime.service_name == "market-data-test"
    assert runtime.stream_key == "stream:test"
    assert runtime.metrics_period == pytest.approx(0.01)
    assert metrics_started.is_set()
    assert not metrics_cancelled.is_set()

    await runtime.shutdown()
    assert metrics_cancelled.is_set()


async def test_startup_twice_raises(provider: StubProvider) -> None:
    async def provider_factory() -> StubProvider:
        return provider

    runtime = MarketDataRuntime(config_provider_factory=provider_factory)

    await runtime.startup()
    with pytest.raises(RuntimeError):
        await runtime.startup()
    await runtime.shutdown()


async def test_shutdown_cleans_up(provider: StubProvider) -> None:
    listener = StubListener()

    def listener_factory(config: MarketDataServiceConfig, runtime: MarketDataRuntime) -> StubListener:
        return listener

    async def provider_factory() -> StubProvider:
        return provider

    runtime = MarketDataRuntime(
        config_provider_factory=provider_factory,
        listener_factory=listener_factory,
    )

    await runtime.startup()
    await runtime.shutdown()

    assert listener.stopped.is_set()
    assert runtime.listener is None
    assert runtime.metrics_task is None
    assert provider.stopped is True
