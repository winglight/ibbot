"""Compatibility wrapper around the risk service HTTP client."""

from __future__ import annotations

from typing import Any, Mapping

from loguru import logger

from src.routes.service_clients import (
    RiskServiceClient,
    ServiceClientError,
    ServiceResponseError,
    ServiceUnavailableError,
)

risk_logger = logger.bind(module="risk_manager", request_id="-")


class RiskManager:
    """Proxy class that forwards operations to :class:`RiskServiceClient`."""

    def __init__(self, *, client: RiskServiceClient | None = None) -> None:
        self._client = client or RiskServiceClient()

    async def _call(self, action: str, **kwargs: Any) -> Any:
        try:
            method = getattr(self._client, action)
        except AttributeError:
            raise AttributeError(f"RiskServiceClient has no action '{action}'")
        try:
            return await method(**kwargs)
        except ServiceUnavailableError as exc:
            risk_logger.warning("Risk service unavailable", action=action, error=str(exc))
            raise
        except ServiceResponseError as exc:
            detail = exc.detail or "risk service error"
            risk_logger.warning("Risk service error", action=action, detail=detail)
            raise
        except ServiceClientError as exc:
            risk_logger.warning("Risk service request failed", action=action, error=str(exc))
            raise

    async def list_rules(self) -> Any:
        return await self._call("list_rules")

    async def create_rule(self, payload: Mapping[str, Any]) -> Any:
        return await self._call("create_rule", payload=payload)

    async def update_rule(
        self,
        symbol: str,
        payload: Mapping[str, Any] | None = None,
        *,
        enabled: bool | None = None,
    ) -> Any:
        return await self._call(
            "update_rule", symbol=symbol, payload=payload, enabled=enabled
        )

    async def delete_rule(self, symbol: str) -> Any:
        return await self._call("delete_rule", symbol=symbol)

    async def get_config(self) -> Any:
        return await self._call("get_config")

    async def update_config(self, payload: Mapping[str, Any]) -> Any:
        return await self._call("update_config", payload=payload)

    async def position_sizing(self, payload: Mapping[str, Any]) -> Any:
        return await self._call("position_sizing", payload=payload)

    async def fetch_sizing_history(
        self,
        *,
        symbol: str | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> Any:
        return await self._call(
            "fetch_sizing_history",
            symbol=symbol,
            since=since,
            limit=int(limit),
        )

    async def fetch_order_queue_status(self) -> Any:
        return await self._call("fetch_order_queue_status")

    async def fetch_config_rules(self) -> Any:
        return await self._call("fetch_config_rules")

    async def update_config_rules(self, payload: Mapping[str, Any]) -> Any:
        return await self._call("update_config_rules", payload=payload)

    async def fetch_rules_history(
        self, *, symbol: str | None = None, limit: int = 100
    ) -> Any:
        return await self._call(
            "fetch_rules_history",
            symbol=symbol,
            limit=int(limit),
        )
