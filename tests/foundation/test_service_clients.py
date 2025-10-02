import json
from typing import Any, Dict

import httpx
import pytest

from src.routes.service_clients import (
    AccountServiceClient,
    AccountServiceResult,
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

    result = await client.fetch_info()

    assert isinstance(result, AccountServiceResult)
    assert result.data == {"balance": 123}
    assert result.metadata == {}


@pytest.mark.anyio("asyncio")
async def test_account_service_positions_handles_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ACCOUNT_SERVICE_URL", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["fast"] == "true"
        return httpx.Response(200, json={"positions": [1, 2, 3]})

    client = AccountServiceClient(transport=httpx.MockTransport(handler), base_url="http://svc")

    result = await client.fetch_positions(fast=True)

    assert isinstance(result, AccountServiceResult)
    assert result.data == [1, 2, 3]
    assert result.metadata == {}


@pytest.mark.anyio("asyncio")
async def test_account_service_omits_false_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ACCOUNT_SERVICE_URL", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert "fast" not in request.url.params
        assert "force_refresh" not in request.url.params
        return httpx.Response(200, json={"data": {"ok": True}})

    client = AccountServiceClient(transport=httpx.MockTransport(handler), base_url="http://svc")

    result = await client.fetch_info(force_refresh=False)

    assert isinstance(result, AccountServiceResult)
    assert result.data == {"ok": True}


@pytest.mark.anyio("asyncio")
async def test_account_service_fetch_info_supports_fast_and_force(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ACCOUNT_SERVICE_URL", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["fast"] == "true"
        assert request.url.params["force_refresh"] == "true"
        return httpx.Response(200, json={"account_info": {"balance": 1}})

    client = AccountServiceClient(transport=httpx.MockTransport(handler), base_url="http://svc")

    result = await client.fetch_info(force_refresh=True, fast=True)

    assert isinstance(result, AccountServiceResult)
    assert result.data == {"balance": 1}


@pytest.mark.anyio("asyncio")
async def test_account_service_fetch_orders_includes_optional_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ACCOUNT_SERVICE_URL", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        assert params["limit"] == "25"
        assert params["offset"] == "0"
        assert params["fast"] == "true"
        assert params["force_refresh"] == "true"
        return httpx.Response(200, json=[])

    client = AccountServiceClient(transport=httpx.MockTransport(handler), base_url="http://svc")

    payload = await client.fetch_orders(
        limit=25,
        offset=0,
        fast=True,
        force_refresh=True,
    )

    assert isinstance(payload, list)
    assert payload == []


@pytest.mark.anyio("asyncio")
async def test_account_service_fetch_daily_summary_with_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ACCOUNT_SERVICE_URL", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        assert params["days"] == "7"
        assert params["fast"] == "true"
        assert params["force_refresh"] == "true"
        return httpx.Response(
            200,
            json={
                "daily_summary": [{"pnl": 1.0}],
                "metadata": {"from_cache": False, "snapshot_updated_at": "2024-01-01T00:00:00"},
            },
        )

    client = AccountServiceClient(transport=httpx.MockTransport(handler), base_url="http://svc")

    result = await client.fetch_daily_summary(days=7, fast=True, force_refresh=True)

    assert isinstance(result, AccountServiceResult)
    assert result.data == [{"pnl": 1.0}]
    assert result.metadata.get("from_cache") is False


@pytest.mark.anyio("asyncio")
async def test_risk_service_create_rule_invokes_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RISK_SERVICE_URL", raising=False)
    captured: Dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["payload"] = json.loads(request.content.decode())
        return httpx.Response(201, json={"success": True})

    client = RiskServiceClient(transport=httpx.MockTransport(handler), base_url="http://risk")

    result = await client.create_rule({"symbol": "ES"})

    assert captured == {
        "method": "POST",
        "path": "/risk/rules",
        "payload": {"symbol": "ES"},
    }
    assert result == {"success": True}


@pytest.mark.anyio("asyncio")
async def test_risk_service_history_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RISK_SERVICE_URL", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sizing/history"):
            assert request.url.params["symbol"] == "ES"
            assert request.url.params["limit"] == "50"
        elif request.url.path.endswith("/risk/rules/history"):
            assert request.url.params["limit"] == "10"
            assert request.url.params["symbol"] == "NQ"
        return httpx.Response(200, json={"success": True})

    client = RiskServiceClient(transport=httpx.MockTransport(handler), base_url="http://risk")

    sizing = await client.fetch_sizing_history(symbol="ES", limit=50)
    rules = await client.fetch_rules_history(symbol="NQ", limit=10)

    assert sizing == {"success": True}
    assert rules == {"success": True}


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

