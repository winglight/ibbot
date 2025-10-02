import json
from typing import Any, Dict

import httpx
import pytest

from src.routes.service_clients import (
    AccountServiceClient,
    RiskServiceClient,
    ServiceClient,
    ServiceResponseError,
    ServiceUnavailableError,
)
from src.ibbot.redis import read_hash


class ExampleServiceClient(ServiceClient):
    service_name = "example"
    env_prefix = "EXAMPLE"
    default_base_url = "http://localhost:9001"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio("asyncio")
async def test_request_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXAMPLE_SERVICE_URL", "http://example.test/api")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
    client = ExampleServiceClient(transport=transport)

    payload = await client.request("GET", "/health")

    assert payload == {"ok": True}
    assert client.base_url == "http://example.test/api"


@pytest.mark.anyio("asyncio")
async def test_request_empty_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXAMPLE_SERVICE_URL", "http://example.test")
    transport = httpx.MockTransport(lambda request: httpx.Response(204))
    client = ExampleServiceClient(transport=transport)

    payload = await client.request("GET", "/empty")

    assert payload is None


@pytest.mark.anyio("asyncio")
async def test_request_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXAMPLE_SERVICE_URL", "http://example.test")

    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    client = ExampleServiceClient(transport=httpx.MockTransport(handler))

    with pytest.raises(ServiceUnavailableError):
        await client.request("GET", "/timeout")


@pytest.mark.anyio("asyncio")
async def test_request_error_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXAMPLE_SERVICE_URL", "http://example.test")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"detail": "bad gateway"})

    client = ExampleServiceClient(transport=httpx.MockTransport(handler))

    with pytest.raises(ServiceResponseError) as excinfo:
        await client.request("GET", "/fail")

    assert excinfo.value.status_code == 502
    assert excinfo.value.detail == "bad gateway"


@pytest.mark.anyio("asyncio")
async def test_account_service_extracts_nested_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ACCOUNT_SERVICE_URL", raising=False)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"account_info": {"balance": 123}})

    client = AccountServiceClient(transport=httpx.MockTransport(handler), base_url="http://svc")

    payload = await client.fetch_info()

    assert payload == {"balance": 123}


@pytest.mark.anyio("asyncio")
async def test_account_service_positions_handles_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ACCOUNT_SERVICE_URL", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["fast"] == "true"
        return httpx.Response(200, json={"positions": [1, 2, 3]})

    client = AccountServiceClient(transport=httpx.MockTransport(handler), base_url="http://svc")

    positions = await client.fetch_positions(fast=True)

    assert positions == [1, 2, 3]


@pytest.mark.anyio("asyncio")
async def test_account_service_refresh_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ACCOUNT_SERVICE_URL", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/account/refresh"
        return httpx.Response(200, json={"success": True})

    client = AccountServiceClient(transport=httpx.MockTransport(handler), base_url="http://svc")

    result = await client.request_refresh()

    assert result is True


@pytest.mark.anyio("asyncio")
async def test_risk_service_post_rule_invokes_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RISK_SERVICE_URL", raising=False)
    captured: Dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["payload"] = json.loads(request.content.decode())
        return httpx.Response(201, json={"success": True})

    client = RiskServiceClient(transport=httpx.MockTransport(handler), base_url="http://risk")

    result = await client.post_rule({"symbol": "ES"})

    assert captured == {
        "method": "POST",
        "path": "/risk/rules",
        "payload": {"symbol": "ES"},
    }
    assert result == {"success": True}


@pytest.mark.anyio("asyncio")
async def test_read_hash_normalises_bytes() -> None:
    class DummyRedis:
        def __init__(self) -> None:
            self._data = {
                "hash": {b"field": b"value", "other": 2},
            }

        async def hgetall(self, key: str) -> Dict[Any, Any]:
            return dict(self._data.get(key, {}))

    payload = await read_hash("hash", redis=DummyRedis())

    assert payload == {"field": "value", "other": 2}

