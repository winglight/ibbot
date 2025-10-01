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


class _CapturingRedis:
    last_from_url: dict[str, object] | None = None
    last_init: dict[str, object] | None = None

    def __init__(self, *args: object, **kwargs: object) -> None:
        type(self).last_init = {"args": args, "kwargs": kwargs}

    @classmethod
    def from_url(cls, url: str, **kwargs: object) -> "_CapturingRedis":
        cls.last_from_url = {"url": url, "kwargs": kwargs}
        return cls()

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:  # pragma: no cover - not exercised
        pass


def test_init_redis_uses_ibbot_redis_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache, "Redis", _CapturingRedis, raising=False)
    monkeypatch.setattr(cache, "_client", None, raising=False)
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("IBBOT_REDIS_URL", "redis://:secret@redis:6379/0")

    result = asyncio.run(cache.init_redis(force=True))

    assert isinstance(result, _CapturingRedis)
    assert _CapturingRedis.last_from_url is not None
    assert _CapturingRedis.last_from_url["url"] == "redis://:secret@redis:6379/0"
    kwargs = _CapturingRedis.last_from_url["kwargs"]
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["decode_responses"] is True
    assert "socket_connect_timeout" in kwargs
    assert _CapturingRedis.last_init == {"args": (), "kwargs": {}}
