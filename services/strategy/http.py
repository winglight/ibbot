"""HTTP shim exposing strategy configuration snapshots."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from fastapi import FastAPI, status

from src.ibbot.config import ConfigProvider

LOGGER = logging.getLogger("ibbot.services.strategy.http")
SERVICE_NAME = "strategy"


def _resolve_redis_url() -> str | None:
    for key in ("STRATEGY_REDIS_URL", "IBBOT_REDIS_URL"):
        value = os.getenv(key, "").strip()
        if value:
            return value
    return None


def _parse_strategies(config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw = config.get("strategies")
    if not raw:
        return {}
    data: Mapping[str, Any]
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            LOGGER.warning("Failed to decode strategy configuration JSON")
            return {}
        if not isinstance(parsed, Mapping):
            return {}
        data = parsed
    elif isinstance(raw, Mapping):
        data = raw
    else:
        return {}

    result: dict[str, dict[str, Any]] = {}
    for name, spec in data.items():
        entry: dict[str, Any] = {"name": str(name)}
        enabled = True
        params: Mapping[str, Any] | None = None
        factory: Any = None
        if isinstance(spec, Mapping):
            enabled = bool(spec.get("enabled", True))
            params = spec.get("params") or spec.get("config")
            factory = spec.get("factory") or spec.get("callable")
        else:
            factory = spec
        entry["enabled"] = enabled
        if factory:
            entry["factory"] = str(factory)
        entry["config"] = dict(params or {})
        result[str(name)] = entry
    return result


@dataclass(slots=True)
class StrategyHttpState:
    provider_factory: Callable[[], ConfigProvider]
    provider: ConfigProvider | None = field(default=None, init=False)
    strategies: dict[str, dict[str, Any]] = field(default_factory=dict, init=False)
    source: str = field(default="config", init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    async def ensure_started(self) -> None:
        if self.provider is None:
            provider = self.provider_factory()
            try:
                await provider.start()
            except Exception:  # pragma: no cover - defensive guard
                LOGGER.warning("Failed to start ConfigProvider for strategy HTTP shim", exc_info=True)
            self.provider = provider
        await self.refresh()

    async def refresh(self) -> None:
        provider = self.provider
        if provider is None:
            return
        try:
            config = await provider.get_module(SERVICE_NAME)
        except Exception:  # pragma: no cover - defensive guard
            LOGGER.warning("Failed to load strategy configuration", exc_info=True)
            return
        strategies = _parse_strategies(config)
        async with self._lock:
            self.strategies = strategies
            source = config.get("source") if isinstance(config, Mapping) else None
            if isinstance(source, str) and source.strip():
                self.source = source

    async def close(self) -> None:
        provider = self.provider
        self.provider = None
        if provider is None:
            return
        try:
            await provider.close()
        except Exception:  # pragma: no cover - defensive guard
            LOGGER.debug("Ignoring ConfigProvider.close() error for strategy shim", exc_info=True)

    async def list_strategies(self) -> list[dict[str, Any]]:
        async with self._lock:
            return [dict(item) for item in self.strategies.values()]

    async def get_strategy(self, name: str) -> dict[str, Any] | None:
        async with self._lock:
            payload = self.strategies.get(name)
            return dict(payload) if payload is not None else None


def _default_provider_factory() -> ConfigProvider:
    redis_url = _resolve_redis_url()
    if redis_url:
        return ConfigProvider(redis_url=redis_url)
    return ConfigProvider()


def create_app(
    *,
    provider_factory: Callable[[], ConfigProvider] | None = None,
) -> FastAPI:
    state = StrategyHttpState(provider_factory=provider_factory or _default_provider_factory)

    app = FastAPI(title="Strategy Service Shim", version="1.0.0")

    @app.on_event("startup")
    async def _startup() -> None:  # pragma: no cover - exercised via TestClient
        await state.ensure_started()

    @app.on_event("shutdown")
    async def _shutdown() -> None:  # pragma: no cover - exercised via TestClient
        await state.close()

    @app.get("/strategy/list", status_code=status.HTTP_200_OK)
    async def list_strategies() -> dict[str, Any]:
        await state.ensure_started()
        strategies = await state.list_strategies()
        if not strategies:
            return {"success": False, "strategies": [], "error": "Strategy configuration unavailable"}
        return {"success": True, "strategies": strategies, "source": state.source}

    @app.get("/strategy/{name}", status_code=status.HTTP_200_OK)
    async def fetch_strategy(name: str) -> dict[str, Any]:
        await state.ensure_started()
        payload = await state.get_strategy(name)
        if payload is None:
            return {"success": False, "error": "Strategy not found", "strategy": None}
        return {"success": True, "strategy": payload, "source": state.source}

    @app.post("/strategy", status_code=status.HTTP_200_OK)
    async def create_strategy(_: Mapping[str, Any]) -> dict[str, Any]:
        return {"success": False, "error": "Strategy runtime shim is read-only"}

    @app.put("/strategy/{name}", status_code=status.HTTP_200_OK)
    async def update_strategy(name: str, _: Mapping[str, Any]) -> dict[str, Any]:
        return {"success": False, "error": "Strategy runtime shim is read-only", "name": name}

    return app


app = create_app()


def main() -> None:  # pragma: no cover - manual execution helper
    import uvicorn

    uvicorn.run("services.strategy.http:app", host="0.0.0.0", port=8045, factory=False)


if __name__ == "__main__":  # pragma: no cover - manual execution helper
    main()
