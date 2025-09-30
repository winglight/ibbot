"""Integration tests for FastAPI startup initialisation."""

from __future__ import annotations

import json
from typing import Any, Dict

import pytest

import core
from core.container import HealthResult
from main import app


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeRedis:
    def __init__(self) -> None:
        self._data: Dict[str, Dict[str, str]] = {}

    async def ping(self) -> bool:  # pragma: no cover - behaviour trivial
        return True

    async def hset(self, key: str, mapping: Dict[str, str]) -> None:
        bucket = self._data.setdefault(key, {})
        bucket.update({k: str(v) for k, v in mapping.items()})

    async def hgetall(self, key: str) -> Dict[str, str]:
        return dict(self._data.get(key, {}))

    async def close(self) -> None:  # pragma: no cover - compatibility hook
        self._data.clear()


class DummyProvider:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._started = False

    async def start(self) -> None:
        self._started = True

    async def close(self) -> None:
        self._started = False

    async def get_module(self, module: str, *, use_cache: bool = True) -> Dict[str, str]:
        assert module == "services"
        return {"health_endpoints": json.dumps({"notifications": "http://notification/health"})}


@pytest.mark.anyio("asyncio")
async def test_startup_initialises_core_and_records_health(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_redis = FakeRedis()

    async def fake_init_redis(*args: Any, **kwargs: Any) -> FakeRedis:
        return fake_redis

    async def fake_probe_health(module: str, url: str) -> HealthResult:
        assert module == "notifications"
        assert url == "http://notification/health"
        return HealthResult(
            module=module,
            healthy=True,
            status_code=200,
            latency_ms=12.5,
            response={"healthy": True},
        )

    monkeypatch.setattr("core.container.init_redis", fake_init_redis)
    monkeypatch.setattr("core.container.configure_influx", lambda **_: None)
    monkeypatch.setattr("core.container.ConfigProvider", DummyProvider)
    monkeypatch.setattr("core.container._probe_health", fake_probe_health)

    # Ensure a clean slate before the test.
    await core.shutdown()

    async with app.router.lifespan_context(app):
        container = core.require_container()
        assert container.redis_client is fake_redis
        result = await fake_redis.hgetall("hash:service:health:notifications")
        assert result["status"] == "up"
        assert json.loads(result["response"]) == {"healthy": True}

    await core.shutdown()
