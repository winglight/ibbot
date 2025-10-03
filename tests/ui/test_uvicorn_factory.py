"""Smoke tests for running the FastAPI app via uvicorn factory mode."""

from __future__ import annotations

import asyncio

import pytest
import uvicorn

import core
from tests.ui.test_startup import DummyProvider, FakeRedis


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio("asyncio")
async def test_uvicorn_factory_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_redis = FakeRedis()

    async def fake_init_redis(*args, **kwargs):  # type: ignore[no-untyped-def]
        return fake_redis

    monkeypatch.setattr("core.container.init_redis", fake_init_redis)
    monkeypatch.setattr(
        "core.container.get_database_url",
        lambda: "mysql+asyncmy://user:pass@localhost/db",
    )
    monkeypatch.setattr("core.container.ConfigProvider", DummyProvider)

    await core.shutdown()

    config = uvicorn.Config(
        "main:app",
        factory=True,
        host="127.0.0.1",
        port=0,
        log_level="warning",
        lifespan="on",
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # type: ignore[assignment]

    serve_task = asyncio.create_task(server.serve())
    try:
        for _ in range(50):
            if server.started:
                break
            if serve_task.done():
                serve_task.result()
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("uvicorn server did not start in time")
    finally:
        server.should_exit = True
        await asyncio.wait_for(serve_task, timeout=5)

    await core.shutdown()
