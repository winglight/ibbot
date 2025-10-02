"""Lightweight client wrapper for strategy service interactions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from loguru import logger

from src.routes.service_clients import (
    ServiceClientError,
    ServiceResponseError,
    ServiceUnavailableError,
    StrategyServiceClient,
)

strategy_logger = logger.bind(module="strategy_manager", request_id="-")


@dataclass
class Strategy:
    name: str
    strategy_type: str
    enabled: bool = True
    description: str | None = None
    api_endpoint: str | None = None
    file_path: str | None = None


class StrategyManager:
    """Compatibility layer that forwards requests to the strategy service."""

    def __init__(self, *, client: StrategyServiceClient | None = None) -> None:
        self._client = client or StrategyServiceClient()

    def set_dependencies(self, **_: Any) -> None:  # pragma: no cover - compatibility
        return None

    def start(self) -> None:  # pragma: no cover - compatibility
        strategy_logger.info("StrategyManager proxy initialised")

    def stop(self) -> None:  # pragma: no cover - compatibility
        return None

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        try:
            func = getattr(self._client, method)
        except AttributeError:
            raise AttributeError(f"StrategyServiceClient has no method '{method}'")
        try:
            return await func(*args, **kwargs)
        except ServiceUnavailableError as exc:
            strategy_logger.warning("Strategy service unavailable", method=method, error=str(exc))
            raise
        except ServiceResponseError as exc:
            detail = exc.detail or "strategy service error"
            strategy_logger.warning("Strategy service error", method=method, detail=detail)
            raise
        except ServiceClientError as exc:
            strategy_logger.warning("Strategy service request failed", method=method, error=str(exc))
            raise

    async def list_strategies(self) -> Any:
        return await self._call("list_strategies")

    async def get_strategy(self, name: str) -> Any:
        return await self._call("get_strategy", name)

    async def create_strategy(self, payload: Mapping[str, Any]) -> Any:
        return await self._call("create_strategy", payload)

    async def update_strategy(self, name: str, updates: Mapping[str, Any]) -> Any:
        return await self._call("update_strategy", name, updates=updates)

    async def delete_strategy(self, name: str) -> Any:
        return await self._call("delete_strategy", name)

    async def toggle_strategy(self, name: str, *, enabled: bool) -> Any:
        return await self._call("toggle_strategy", name, enabled=enabled)

    async def request_data_push(self, name: str, payload: Mapping[str, Any]) -> Any:
        return await self._call("request_data_push", name, payload)
