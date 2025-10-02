"""OpenAPI aggregation and documentation helper routes."""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Sequence

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from loguru import logger

from src.ibbot.redis import get_redis_client
from src.routes.service_clients import (
    AccountServiceClient,
    ConnectionControlClient,
    MarketDataServiceClient,
    NotificationServiceClient,
    OrdersServiceClient,
    RiskServiceClient,
    ServiceClient,
    StrategyServiceClient,
)


MERGED_DOCS_CACHE_KEY = os.getenv("API_DOCS_CACHE_KEY", "cache:openapi:merged")
DEFAULT_CACHE_TTL = int(os.getenv("API_DOCS_CACHE_TTL", "300") or 0)
MERGED_SPEC_ROUTE = "/api/docs/merged"

router = APIRouter()

__all__ = [
    "router",
    "generate_merged_openapi",
    "MERGED_DOCS_CACHE_KEY",
    "SERVICE_SPEC_TARGETS",
    "ServiceSpecTarget",
]


@dataclass(slots=True)
class ServiceSpecTarget:
    """Configuration describing a service OpenAPI endpoint."""

    name: str
    title: str
    factory: Callable[[], ServiceClient]
    openapi_path: str = "/openapi.json"

    def create(self) -> ServiceClient:
        return self.factory()


SERVICE_SPEC_TARGETS: Sequence[ServiceSpecTarget] = (
    ServiceSpecTarget("account", "Account Service", AccountServiceClient),
    ServiceSpecTarget("orders", "Orders Service", OrdersServiceClient),
    ServiceSpecTarget("risk", "Risk Service", RiskServiceClient),
    ServiceSpecTarget("strategy", "Strategy Service", StrategyServiceClient),
    ServiceSpecTarget("market_data", "Market Data Service", MarketDataServiceClient),
    ServiceSpecTarget(
        "notification",
        "Notification Service",
        NotificationServiceClient,
    ),
    ServiceSpecTarget(
        "connection_control",
        "Connection Control Service",
        ConnectionControlClient,
    ),
)


_VALID_METHODS = {"get", "post", "put", "delete", "patch", "options", "head"}


def _deepcopy_schema(schema: Mapping[str, Any]) -> MutableMapping[str, Any]:
    return copy.deepcopy(schema)


def _normalise_servers(servers: Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if not servers:
        return result
    for server in servers:
        try:
            url = str(server.get("url", "")).strip()
        except AttributeError:  # pragma: no cover - defensive
            continue
        if not url:
            continue
        entry: dict[str, Any] = {"url": url}
        description = server.get("description")
        if description:
            entry["description"] = str(description)
        if entry not in result:
            result.append(entry)
    return result


def _merge_tags(
    base_schema: MutableMapping[str, Any],
    service_tags: Iterable[Mapping[str, Any]] | None,
    *,
    service_title: str,
) -> None:
    tags: list[MutableMapping[str, Any]] = list(base_schema.setdefault("tags", []))
    known = {tag.get("name") for tag in tags if isinstance(tag, Mapping)}
    if service_title not in known:
        tags.append({"name": service_title, "description": f"Endpoints served by {service_title}."})
        known.add(service_title)
    if not service_tags:
        base_schema["tags"] = tags
        return
    for tag in service_tags:
        if not isinstance(tag, Mapping):
            continue
        name = tag.get("name")
        if not name or name in known:
            continue
        tags.append(dict(tag))
        known.add(name)
    base_schema["tags"] = tags


def _merge_paths(
    base_schema: MutableMapping[str, Any],
    service_schema: Mapping[str, Any],
    *,
    service_title: str,
    warnings: list[str],
) -> None:
    base_paths: MutableMapping[str, Any] = base_schema.setdefault("paths", {})  # type: ignore[assignment]
    service_paths = service_schema.get("paths")
    if not isinstance(service_paths, Mapping):
        return
    for raw_path, raw_methods in service_paths.items():
        if not isinstance(raw_path, str) or not isinstance(raw_methods, Mapping):
            continue
        target_methods: MutableMapping[str, Any] = base_paths.setdefault(raw_path, {})  # type: ignore[assignment]
        for method, operation in raw_methods.items():
            method_name = str(method).lower()
            if method_name not in _VALID_METHODS:
                continue
            if method_name in target_methods:
                warnings.append(
                    f"Skipped {service_title} operation {method_name.upper()} {raw_path} due to conflict with existing schema."
                )
                continue
            op_payload = copy.deepcopy(operation)
            if isinstance(op_payload, Mapping):
                op_tags = op_payload.setdefault("tags", [])  # type: ignore[assignment]
                if isinstance(op_tags, list) and service_title not in op_tags:
                    op_tags.append(service_title)
            target_methods[method_name] = op_payload


def _merge_components(
    base_schema: MutableMapping[str, Any],
    service_schema: Mapping[str, Any],
    *,
    service_title: str,
    warnings: list[str],
) -> None:
    service_components = service_schema.get("components")
    if not isinstance(service_components, Mapping):
        return
    base_components: MutableMapping[str, Any] = base_schema.setdefault("components", {})  # type: ignore[assignment]
    for section, payload in service_components.items():
        if not isinstance(section, str) or not isinstance(payload, Mapping):
            continue
        target_section: MutableMapping[str, Any] = base_components.setdefault(section, {})  # type: ignore[assignment]
        for name, definition in payload.items():
            if not isinstance(name, str):
                continue
            if name in target_section and target_section[name] != definition:
                warnings.append(
                    f"Skipped {service_title} component '{section}.{name}' due to conflict with existing schema."
                )
                continue
            target_section[name] = copy.deepcopy(definition)


def _merge_servers(
    base_schema: MutableMapping[str, Any],
    additional_servers: Iterable[Mapping[str, Any]] | None,
) -> None:
    if not additional_servers:
        return
    servers = base_schema.setdefault("servers", [])  # type: ignore[assignment]
    existing = {(entry.get("url"), entry.get("description")) for entry in servers if isinstance(entry, Mapping)}
    for server in additional_servers:
        if not isinstance(server, Mapping):
            continue
        url = str(server.get("url", "")).strip()
        if not url:
            continue
        description = server.get("description")
        key = (url, description)
        if key in existing:
            continue
        entry: dict[str, Any] = {"url": url}
        if description:
            entry["description"] = description
        servers.append(entry)
        existing.add(key)


def _service_metadata_entry(
    *,
    name: str,
    title: str,
    spec_url: str,
    server_url: str | None,
    status: str,
    warning: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": name,
        "title": title,
        "spec_url": spec_url,
        "status": status,
    }
    if server_url:
        entry["server_url"] = server_url
    if warning:
        entry["warning"] = warning
    return entry


async def _fetch_service_spec(
    client: httpx.AsyncClient,
    *,
    target: ServiceSpecTarget,
) -> tuple[Mapping[str, Any] | None, str | None, str | None]:
    instance = target.create()
    base_url = getattr(instance, "base_url", "").rstrip("/")
    if not base_url:
        return None, "missing base URL", None
    spec_url = f"{base_url}{target.openapi_path}"
    try:
        response = await client.get(spec_url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.debug("Failed to fetch %s OpenAPI schema: %s", target.name, exc)
        return None, str(exc), base_url
    try:
        data = response.json()
    except ValueError as exc:
        logger.debug("Invalid OpenAPI payload from %s: %s", target.name, exc)
        return None, "invalid JSON payload", base_url
    if not isinstance(data, Mapping):
        return None, "unexpected payload structure", base_url
    return data, None, base_url


async def _collect_service_specs(
    *,
    targets: Sequence[ServiceSpecTarget],
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[
    list[tuple[ServiceSpecTarget, Mapping[str, Any], str | None]],
    list[tuple[ServiceSpecTarget, str, str | None]],
]:
    timeout = float(os.getenv("API_DOCS_FETCH_TIMEOUT", "5"))
    successes: list[tuple[ServiceSpecTarget, Mapping[str, Any], str | None]] = []
    failures: list[tuple[ServiceSpecTarget, str, str | None]] = []
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        for target in targets:
            spec, warning, base_url = await _fetch_service_spec(client, target=target)
            if spec is None:
                failures.append((target, warning or "failed to fetch", base_url))
            else:
                successes.append((target, spec, base_url))
    return successes, failures


def _prepare_metadata(
    *,
    base_schema: MutableMapping[str, Any],
    successes: Iterable[tuple[ServiceSpecTarget, Mapping[str, Any], str | None]],
    failures: Iterable[tuple[ServiceSpecTarget, str, str | None]],
) -> None:
    metadata: MutableMapping[str, Any] = base_schema.setdefault("x-service-metadata", {})  # type: ignore[assignment]
    services: list[dict[str, Any]] = []
    for target, _, base_url in successes:
        services.append(
            _service_metadata_entry(
                name=target.name,
                title=target.title,
                spec_url=f"{base_url}{target.openapi_path}" if base_url else target.openapi_path,
                server_url=base_url,
                status="loaded",
            )
        )
    for target, warning, base_url in failures:
        services.append(
            _service_metadata_entry(
                name=target.name,
                title=target.title,
                spec_url=f"{base_url}{target.openapi_path}" if base_url else target.openapi_path,
                server_url=base_url,
                status="unavailable",
                warning=warning,
            )
        )
    metadata["services"] = sorted(services, key=lambda entry: entry.get("title", ""))
    metadata["generated_at"] = datetime.now(timezone.utc).isoformat()


def _merge_service_schema(
    base_schema: MutableMapping[str, Any],
    *,
    target: ServiceSpecTarget,
    service_schema: Mapping[str, Any],
    warnings: list[str],
    base_url: str | None,
) -> None:
    _merge_tags(base_schema, service_schema.get("tags"), service_title=target.title)
    _merge_paths(base_schema, service_schema, service_title=target.title, warnings=warnings)
    _merge_components(base_schema, service_schema, service_title=target.title, warnings=warnings)
    _merge_servers(base_schema, _normalise_servers(service_schema.get("servers")))
    if base_url:
        _merge_servers(
            base_schema,
            (
                {
                    "url": base_url,
                    "description": f"{target.title} base URL",
                },
            ),
        )


def _store_warnings(
    base_schema: MutableMapping[str, Any],
    warnings: Iterable[str],
) -> None:
    base_schema["x-service-warnings"] = list(warnings)


async def _store_in_cache(
    *,
    redis: Any | None,
    payload: Mapping[str, Any],
) -> None:
    if redis is None:
        return
    ttl = DEFAULT_CACHE_TTL
    try:
        encoded = json.dumps(payload)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        logger.debug("Failed to encode merged OpenAPI schema for caching", exc_info=True)
        return
    try:
        await redis.set(MERGED_DOCS_CACHE_KEY, encoded, ex=ttl or None)
    except Exception:  # pragma: no cover - redis errors are logged
        logger.warning("Failed to store merged OpenAPI schema in Redis", exc_info=True)


async def _load_from_cache(redis: Any | None) -> MutableMapping[str, Any] | None:
    if redis is None:
        return None
    try:
        cached = await redis.get(MERGED_DOCS_CACHE_KEY)
    except Exception:  # pragma: no cover - redis errors are logged
        logger.warning("Failed to load cached OpenAPI schema from Redis", exc_info=True)
        return None
    if not cached:
        return None
    try:
        if isinstance(cached, bytes):
            cached = cached.decode("utf-8")
        data = json.loads(cached)
    except (ValueError, AttributeError):
        logger.debug("Cached OpenAPI schema is not valid JSON; ignoring cached value")
        return None
    if not isinstance(data, Mapping):
        return None
    return copy.deepcopy(data)  # type: ignore[return-value]


async def generate_merged_openapi(
    request: Request,
    *,
    redis: Any | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    force_refresh: bool = False,
) -> MutableMapping[str, Any]:
    """Return the merged OpenAPI schema for the application and services."""

    cached_schema: MutableMapping[str, Any] | None = None
    if not force_refresh:
        cached_schema = await _load_from_cache(redis)
        if cached_schema and not cached_schema.get("x-service-warnings"):
            return cached_schema

    try:
        base_schema = _deepcopy_schema(request.app.openapi())
        warnings: list[str] = []
        successes, failures = await _collect_service_specs(
            targets=list(SERVICE_SPEC_TARGETS),
            transport=transport,
        )
        for target, schema, base_url in successes:
            _merge_service_schema(
                base_schema,
                target=target,
                service_schema=schema,
                warnings=warnings,
                base_url=base_url,
            )
        for target, warning, _ in failures:
            warnings.append(f"{target.title} documentation unavailable: {warning}")

        _prepare_metadata(
            base_schema=base_schema,
            successes=successes,
            failures=failures,
        )
        _store_warnings(base_schema, warnings)

        await _store_in_cache(redis=redis, payload=base_schema)

        return base_schema
    except Exception:
        logger.exception("Failed to build merged OpenAPI schema")
        if cached_schema is not None:
            return cached_schema
        return _deepcopy_schema(request.app.openapi())


async def _resolve_redis_client() -> Any | None:
    try:
        return await get_redis_client()
    except Exception:  # pragma: no cover - redis optional
        logger.debug("Redis client unavailable for OpenAPI caching", exc_info=True)
        return None


@router.get(MERGED_SPEC_ROUTE, include_in_schema=False)
async def get_merged_openapi(request: Request, refresh: str | None = None) -> JSONResponse:
    redis = await _resolve_redis_client()
    force_refresh = refresh is not None and refresh.strip().lower() not in {"0", "false", "no"}
    schema = await generate_merged_openapi(
        request,
        redis=redis,
        force_refresh=force_refresh,
    )
    return JSONResponse(schema)


def _service_spec_entries() -> list[dict[str, str]]:
    entries: list[dict[str, str]] = [
        {
            "name": "merged",
            "title": "All Services",
            "spec_url": MERGED_SPEC_ROUTE,
        },
        {
            "name": "main",
            "title": "Main Application",
            "spec_url": "/api-docs.json",
        },
    ]
    for target in SERVICE_SPEC_TARGETS:
        instance = target.create()
        base_url = getattr(instance, "base_url", "").rstrip("/")
        spec_url = f"{base_url}{target.openapi_path}" if base_url else target.openapi_path
        entries.append(
            {
                "name": target.name,
                "title": target.title,
                "spec_url": spec_url,
            }
        )
    return entries


@router.get("/api/docs/services", include_in_schema=False)
async def list_documentation_services() -> JSONResponse:
    services = _service_spec_entries()
    return JSONResponse({"services": services})

