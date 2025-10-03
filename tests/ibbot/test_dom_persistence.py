from __future__ import annotations

import json
from typing import Any, Dict

import pytest

import src.dom_persistence as dom_persistence


class DummyRedisClient:
    def __init__(self) -> None:
        self.storage: Dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.storage.get(key)

    async def set(self, key: str, value: str) -> None:
        self.storage[key] = value


@pytest.fixture()
def dummy_redis(monkeypatch: pytest.MonkeyPatch) -> DummyRedisClient:
    client = DummyRedisClient()

    async def _get_client(*_: Any, **__: Any) -> DummyRedisClient:  # type: ignore[return-value]
        return client

    monkeypatch.setattr(dom_persistence, "get_redis_client", _get_client)
    return client


def test_load_dom_config_reads_json_payload(dummy_redis: DummyRedisClient) -> None:
    payload = {"window": 123, "max_levels": 15}
    key = "json:dom:test:config"
    dummy_redis.storage[key] = json.dumps(payload)

    loaded = dom_persistence.load_dom_config(redis_key=key)

    assert loaded == payload


def test_save_dom_config_writes_json_payload(dummy_redis: DummyRedisClient) -> None:
    key = "json:dom:test:config:save"
    payload = {"window": 64, "trend_thresholds": [0.5, 0.7, 0.9, 0.95]}

    dom_persistence.save_dom_config(payload, redis_key=key)

    assert key in dummy_redis.storage
    assert json.loads(dummy_redis.storage[key]) == payload
