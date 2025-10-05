"""Tests for the MariaDB-backed configuration provider."""

from __future__ import annotations

import asyncio
import os
from typing import Any, Callable, Dict

import pytest

from src.config import load_project_env
from src.db import ConfigRepository, ConfigRepositoryError
from src.ibbot.config import ConfigProvider


async def wait_until(condition: Callable[[], bool], timeout: float = 1.0) -> None:
    """Utility helper to wait for a condition to become truthy."""

    end_time = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end_time:
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


class FakeRedis:
    """Minimal Redis subset used for publish assertions in tests."""

    def __init__(self) -> None:
        self._hashes: Dict[str, Dict[str, str]] = {}
        self.published: list[tuple[str, Any]] = []

    async def hgetall(self, key: str) -> Dict[str, str]:
        return dict(self._hashes.get(key, {}))

    async def hset(self, key: str, mapping: Dict[str, Any]) -> None:
        bucket = self._hashes.setdefault(key, {})
        for field, value in mapping.items():
            bucket[str(field)] = str(value)

    async def delete(self, key: str) -> None:
        self._hashes.pop(key, None)

    async def publish(self, channel: str, data: Any) -> None:
        self.published.append((channel, data))

    async def close(self) -> None:  # pragma: no cover - compatibility shim
        return None


class FailingRepository:
    """Repository stub that always raises errors."""

    async def load_module(self, module: str):  # pragma: no cover - used for fallback tests
        raise ConfigRepositoryError("database unavailable")

    async def save_module(self, module: str, values: Dict[str, str]):  # pragma: no cover - error path
        raise ConfigRepositoryError("database unavailable")

    async def load_versions(self) -> Dict[str, str]:  # pragma: no cover - fallback path
        raise ConfigRepositoryError("database unavailable")


def test_config_provider_merges_env_and_db(sqlite_db, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        load_project_env.cache_clear()
        monkeypatch.setenv("STOP_LOSS_RATIO", "0.05")

        repository = ConfigRepository()
        provider = ConfigProvider(config_repository=repository)

        merged = await provider.get_module("risk")
        assert merged["STOP_LOSS_RATIO"] == "0.05"

        await provider.set_module("risk", {"STOP_LOSS_RATIO": "0.07", "EXTRA": "abc"})

        assert await provider.get("risk", "STOP_LOSS_RATIO", type_=float) == pytest.approx(0.07)
        assert await provider.get("risk", "EXTRA") == "abc"
        assert await provider.get("risk", "UNKNOWN", default="fallback") == "fallback"

        await provider.close()

    asyncio.run(scenario())


def test_config_provider_detects_external_db_changes(sqlite_db, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        load_project_env.cache_clear()
        monkeypatch.setenv("STOP_LOSS_RATIO", "0.05")

        repository = ConfigRepository()
        provider = ConfigProvider(config_repository=repository, poll_interval=0.05)

        updates: list[tuple[str, str]] = []

        async def listener(module: str, payload: Dict[str, str]) -> None:
            updates.append((module, payload["STOP_LOSS_RATIO"]))

        provider.subscribe("risk", listener)

        await provider.start()
        try:
            await provider.get_module("risk")
            await repository.save_module("risk", {"STOP_LOSS_RATIO": "0.12"})
            await wait_until(lambda: updates, timeout=1.5)
            final_value = await provider.get("risk", "STOP_LOSS_RATIO", type_=float)
        finally:
            await provider.close()

        assert updates == [("risk", "0.12")]
        assert final_value == pytest.approx(0.12)

    asyncio.run(scenario())


def test_config_provider_raises_when_db_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        load_project_env.cache_clear()
        monkeypatch.delenv("STOP_LOSS_RATIO", raising=False)

        fake = FakeRedis()
        provider = ConfigProvider(redis_client=fake, config_repository=FailingRepository())

        with pytest.raises(ConfigRepositoryError):
            await provider.get_module("risk")

        with pytest.raises(ConfigRepositoryError):
            await provider.set_module("risk", {"STOP_LOSS_RATIO": "0.2"})

        assert fake._hashes == {}
        assert fake.published == []

        await provider.close()

    asyncio.run(scenario())
