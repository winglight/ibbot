"""Lightweight HTTP interface exposing risk runtime snapshots.

The historical implementation exposed a richer API capable of mutating
configuration.  For the main UI we only require read access to the latest
rule/config snapshots produced by :class:`RiskRuntime`.  This module provides
that functionality while keeping the responses stable even when Redis (or the
runtime itself) is temporarily unavailable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, MutableMapping

from fastapi import FastAPI, status

from services.risk.runtime import RiskRuntime
from src.ibbot.redis import get_redis_client

try:  # Optional dependency during some tests.
    from redis.asyncio import Redis
except Exception:  # pragma: no cover - redis may be missing in CI
    Redis = Any  # type: ignore[misc, assignment]

LOGGER = logging.getLogger("ibbot.services.risk.http")

RISK_RULES_CACHE_KEY = os.getenv("RISK_RULES_CACHE_KEY", "json:risk:rules")
RISK_CONFIG_CACHE_KEY = os.getenv("RISK_CONFIG_CACHE_KEY", "json:risk:config")


async def _maybe_await(result: Any) -> Any:
    if asyncio.iscoroutine(result):
        return await result
    return result


def _decode(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="ignore")
    return str(value)


def _parse_json(raw: Any) -> MutableMapping[str, Any] | None:
    if raw is None:
        return None
    text = _decode(raw).strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return None
    if isinstance(payload, Mapping):
        return dict(payload)
    return None


def _normalise_rules(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    rules = payload.get("rules") or payload.get("data")
    if isinstance(rules, list):
        normalised: list[dict[str, Any]] = []
        for item in rules:
            if isinstance(item, Mapping):
                normalised.append(dict(item))
        return normalised
    if isinstance(rules, Mapping):
        normalised = []
        for symbol, spec in rules.items():
            entry: dict[str, Any]
            if isinstance(spec, Mapping):
                entry = dict(spec)
            else:
                entry = {"value": spec}
            entry.setdefault("symbol", str(symbol))
            normalised.append(entry)
        return normalised
    return []


def _build_success(payload: Mapping[str, Any], *, source: str | None = None) -> dict[str, Any]:
    response = dict(payload)
    response.setdefault("success", True)
    response.setdefault("source", source or "risk-service")
    rules = _normalise_rules(response)
    if rules:
        response["rules"] = rules
        response.setdefault("count", len(rules))
    return response


def _build_failure(message: str, *, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "success": False,
        "error": message,
    }
    if extra:
        payload.update(dict(extra))
    return payload


@dataclass(slots=True)
class RiskHttpState:
    redis_factory: Callable[[], Awaitable[Redis]] = get_redis_client
    runtime: RiskRuntime | None = None
    redis: Redis | None = field(default=None, init=False)

    async def acquire_redis(self) -> None:
        if self.redis is not None:
            return
        try:
            self.redis = await self.redis_factory()
        except Exception:  # pragma: no cover - defensive guard
            LOGGER.warning("Failed to acquire Redis client for risk HTTP shim", exc_info=True)
            self.redis = None

    async def close(self) -> None:
        if self.redis is None:
            return
        try:
            close = getattr(self.redis, "close", None)
            if close is not None:
                await _maybe_await(close())
        except Exception:  # pragma: no cover - defensive guard
            LOGGER.debug("Ignoring redis.close() error for risk HTTP shim", exc_info=True)
        finally:
            self.redis = None

    async def _load_runtime_snapshot(self, method: str) -> MutableMapping[str, Any] | None:
        runtime = self.runtime
        if runtime is None:
            return None
        try:
            handler = getattr(runtime, method)
        except AttributeError:
            return None
        try:
            result = await handler()
        except Exception:  # pragma: no cover - runtime guards
            LOGGER.debug("Risk runtime snapshot method %s failed", method, exc_info=True)
            return None
        if isinstance(result, Mapping):
            return dict(result)
        return None

    async def load_rules_snapshot(self) -> MutableMapping[str, Any] | None:
        runtime_payload = await self._load_runtime_snapshot("get_rules_snapshot")
        if runtime_payload is not None:
            return runtime_payload
        await self.acquire_redis()
        if self.redis is None:
            return None
        try:
            raw = await self.redis.get(RISK_RULES_CACHE_KEY)
        except Exception:  # pragma: no cover - network failures
            LOGGER.warning("Failed to read risk rules snapshot from Redis", exc_info=True)
            return None
        return _parse_json(raw)

    async def load_config_snapshot(self) -> MutableMapping[str, Any] | None:
        runtime_payload = await self._load_runtime_snapshot("get_config_snapshot")
        if runtime_payload is not None:
            return runtime_payload
        await self.acquire_redis()
        if self.redis is None:
            return None
        try:
            raw = await self.redis.get(RISK_CONFIG_CACHE_KEY)
        except Exception:  # pragma: no cover - network failures
            LOGGER.warning("Failed to read risk config snapshot from Redis", exc_info=True)
            return None
        return _parse_json(raw)


def create_app(
    runtime: RiskRuntime | None = None,
    *,
    redis_factory: Callable[[], Awaitable[Redis]] | None = None,
) -> FastAPI:
    """Return a FastAPI application exposing risk snapshots."""

    state = RiskHttpState(runtime=runtime)
    if redis_factory is not None:
        state.redis_factory = redis_factory

    app = FastAPI(title="Risk Service Shim", version="1.0.0")

    @app.on_event("startup")
    async def _startup() -> None:  # pragma: no cover - exercised via TestClient
        await state.acquire_redis()

    @app.on_event("shutdown")
    async def _shutdown() -> None:  # pragma: no cover - exercised via TestClient
        await state.close()

    @app.get("/risk_rules", status_code=status.HTTP_200_OK)
    async def get_risk_rules() -> dict[str, Any]:
        snapshot = await state.load_rules_snapshot()
        if snapshot is None:
            return _build_failure("Risk rules unavailable", extra={"rules": []})
        return _build_success(snapshot, source=snapshot.get("source"))

    @app.get("/risk-config", status_code=status.HTTP_200_OK)
    async def get_risk_config() -> dict[str, Any]:
        snapshot = await state.load_config_snapshot()
        if snapshot is None:
            return _build_failure("Risk configuration unavailable")
        response = dict(snapshot)
        response.setdefault("success", True)
        response.setdefault("source", snapshot.get("source", "risk-service"))
        return response

    @app.post("/risk_rules", status_code=status.HTTP_200_OK)
    async def create_risk_rule(_: Mapping[str, Any]) -> dict[str, Any]:
        return _build_failure("Risk runtime shim is read-only")

    @app.put("/risk_rules/{symbol}", status_code=status.HTTP_200_OK)
    async def update_risk_rule(symbol: str, _: Mapping[str, Any]) -> dict[str, Any]:
        return _build_failure(
            "Risk runtime shim is read-only",
            extra={"symbol": symbol},
        )

    @app.delete("/risk_rules/{symbol}", status_code=status.HTTP_200_OK)
    async def delete_risk_rule(symbol: str) -> dict[str, Any]:
        return _build_failure(
            "Risk runtime shim is read-only",
            extra={"symbol": symbol},
        )

    return app


app = create_app()


def main() -> None:  # pragma: no cover - manual execution helper
    import uvicorn

    uvicorn.run("services.risk.http:app", host="0.0.0.0", port=8030, factory=False)


if __name__ == "__main__":  # pragma: no cover - manual execution helper
    main()
