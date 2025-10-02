import pytest
from fastapi.testclient import TestClient
from typing import Mapping

from services.strategy.http import create_app


class ProviderStub:
    def __init__(self, payload: Mapping[str, str]) -> None:
        self.payload = payload
        self.started = False
        self.closed = False

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def get_module(self, module: str, *, use_cache: bool = True):  # type: ignore[override]
        return self.payload


def test_strategy_http_lists_configured_strategies(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = ProviderStub({"strategies": '{"alpha": {"enabled": true, "params": {"size": 1}}}'})

    def factory() -> ProviderStub:
        return provider

    app = create_app(provider_factory=factory)

    with TestClient(app) as client:
        response = client.get("/strategy/list")

    body = response.json()
    assert response.status_code == 200
    assert body["success"] is True
    assert body["strategies"][0]["name"] == "alpha"
    assert provider.started is True
    assert provider.closed is True


def test_strategy_http_handles_missing_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = ProviderStub({})

    def factory() -> ProviderStub:
        return provider

    app = create_app(provider_factory=factory)

    with TestClient(app) as client:
        response = client.get("/strategy/something")

    body = response.json()
    assert response.status_code == 200
    assert body["success"] is False
    assert body["strategy"] is None
