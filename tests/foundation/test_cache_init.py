import asyncio
import time

import pytest

import src.cache as cache


class _SlowRedis:
    instances: list["_SlowRedis"] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.closed = False
        _SlowRedis.instances.append(self)

    async def ping(self) -> None:
        await asyncio.sleep(10)

    async def close(self) -> None:
        self.closed = True


def test_init_redis_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    _SlowRedis.instances.clear()
    monkeypatch.setattr(cache, "Redis", _SlowRedis, raising=False)
    monkeypatch.setattr(cache, "_client", None, raising=False)
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("IBBOT_REDIS_URL", "")
    monkeypatch.setenv("REDIS_CONNECT_TIMEOUT", "0.1")
    monkeypatch.setenv("REDIS_PING_TIMEOUT", "0.2")

    start = time.monotonic()
    result = asyncio.run(cache.init_redis(force=True))
    elapsed = time.monotonic() - start

    assert result is None
    assert elapsed < 1.0
    assert _SlowRedis.instances and _SlowRedis.instances[0].closed
