import asyncio
import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import Request

from src.routes import api_docs
from src.routes.api_docs import (
    MERGED_DOCS_CACHE_KEY,
    SERVICE_SPEC_TARGETS,
    ServiceSpecTarget,
    generate_merged_openapi,
)


class DummyRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.expire: Any = None

    async def get(self, key: str) -> Any:
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: Any = None) -> bool:
        self.store[key] = value
        self.expire = ex
        return True


class StubClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")


def _build_request(app: FastAPI) -> Request:
    scope = {
        "type": "http",
        "app": app,
        "method": "GET",
        "path": "/api/docs/merged",
    }
    return Request(scope)


@pytest.fixture(autouse=True)
def restore_service_targets(monkeypatch):
    original = SERVICE_SPEC_TARGETS
    yield
    monkeypatch.setattr(api_docs, "SERVICE_SPEC_TARGETS", original)


def test_generate_merged_openapi_handles_service_downtime(monkeypatch):
    async def _run() -> None:
        app = FastAPI()

        @app.get("/hello")
        async def hello():  # pragma: no cover - exercised via OpenAPI generation
            return {"message": "ok"}

        redis = DummyRedis()

        alpha_spec = {
            "openapi": "3.1.0",
            "paths": {
                "/alpha/one": {
                    "get": {
                        "summary": "Alpha ping",
                        "responses": {"200": {"description": "OK"}},
                    }
                }
            },
            "components": {
                "schemas": {
                    "AlphaPayload": {"type": "object", "properties": {"value": {"type": "string"}}}
                }
            },
            "tags": [{"name": "alpha"}],
        }

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "alpha.test":
                return httpx.Response(200, json=alpha_spec)
            if request.url.host == "beta.test":
                raise httpx.ConnectError("service offline", request=request)
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)

        monkeypatch.setattr(
            api_docs,
            "SERVICE_SPEC_TARGETS",
            (
                ServiceSpecTarget("alpha", "Alpha Service", lambda: StubClient("http://alpha.test")),
                ServiceSpecTarget("beta", "Beta Service", lambda: StubClient("http://beta.test")),
            ),
        )

        schema = await generate_merged_openapi(
            _build_request(app),
            redis=redis,
            transport=transport,
            force_refresh=True,
        )

        assert "/hello" in schema.get("paths", {})
        assert "/alpha/one" in schema["paths"]
        alpha_get = schema["paths"]["/alpha/one"].get("get")
        assert alpha_get is not None
        assert "Alpha Service" in alpha_get.get("tags", [])

        warnings = schema.get("x-service-warnings")
        assert isinstance(warnings, list)
        assert any("Beta Service" in warning for warning in warnings)

        metadata = schema.get("x-service-metadata", {})
        services = metadata.get("services", [])
        assert any(entry.get("name") == "alpha" and entry.get("status") == "loaded" for entry in services)
        assert any(entry.get("name") == "beta" and entry.get("status") == "unavailable" for entry in services)

        cached = await redis.get(MERGED_DOCS_CACHE_KEY)
        assert cached is not None
        cached_doc = json.loads(cached)
        assert cached_doc.get("x-service-warnings") == warnings

    asyncio.run(_run())


def test_cached_spec_updates_when_service_recovers(monkeypatch):
    async def _run() -> None:
        app = FastAPI()

        @app.get("/status")
        async def status():  # pragma: no cover - exercised via OpenAPI generation
            return {"status": "ok"}

        redis = DummyRedis()

        beta_spec = {
            "openapi": "3.0.3",
            "paths": {
                "/beta/info": {
                    "get": {
                        "summary": "Beta info",
                        "responses": {"200": {"description": "OK"}},
                    }
                }
            },
        }

        state = {"beta_up": False}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "beta.test":
                if state["beta_up"]:
                    return httpx.Response(200, json=beta_spec)
                raise httpx.ConnectError("beta unavailable", request=request)
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)

        monkeypatch.setattr(
            api_docs,
            "SERVICE_SPEC_TARGETS",
            (
                ServiceSpecTarget("beta", "Beta Service", lambda: StubClient("http://beta.test")),
            ),
        )

        schema_down = await generate_merged_openapi(
            _build_request(app),
            redis=redis,
            transport=transport,
            force_refresh=True,
        )
        warnings_down = schema_down.get("x-service-warnings")
        assert warnings_down and any("Beta Service" in warning for warning in warnings_down)

        state["beta_up"] = True

        schema_up = await generate_merged_openapi(
            _build_request(app),
            redis=redis,
            transport=transport,
            force_refresh=False,
        )

        warnings_up = schema_up.get("x-service-warnings")
        assert warnings_up == []
        assert "/beta/info" in schema_up.get("paths", {})

        cached = await redis.get(MERGED_DOCS_CACHE_KEY)
        assert cached is not None
        cached_doc = json.loads(cached)
        assert cached_doc.get("x-service-warnings") == []
        assert "/beta/info" in cached_doc.get("paths", {})

    asyncio.run(_run())
