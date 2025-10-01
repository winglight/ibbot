"""Reusable async HTTP clients for ibbot service integrations.

This module centralises the logic for talking to the various ibbot
micro-services.  A thin :class:`ServiceClient` base class is responsible for
resolving the base URL and timeout from environment variables (e.g.
``ACCOUNT_SERVICE_URL`` and ``ACCOUNT_SERVICE_TIMEOUT``) and for normalising
error handling so that callers receive domain specific exceptions instead of
``httpx`` primitives.

Domain specific clients such as :class:`AccountServiceClient` or
:class:`RiskServiceClient` expose a handful of convenience methods that wrap
the shared ``request`` coroutine.  The helpers return parsed JSON payloads
while automatically mapping empty responses to ``None``.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Mapping, MutableMapping, Optional

import httpx

__all__ = [
    "ServiceClient",
    "ServiceClientError",
    "ServiceUnavailableError",
    "ServiceResponseError",
    "AccountServiceClient",
    "RiskServiceClient",
    "StrategyServiceClient",
    "MarketDataServiceClient",
    "ConnectionControlClient",
]


class ServiceClientError(RuntimeError):
    """Base exception for service client failures."""

    service: str

    def __init__(self, service: str, message: str) -> None:
        self.service = service
        super().__init__(message)


class ServiceUnavailableError(ServiceClientError):
    """Raised when the target service cannot be reached."""


class ServiceResponseError(ServiceClientError):
    """Raised when the service returned an unexpected HTTP response."""

    def __init__(
        self,
        service: str,
        *,
        status_code: int,
        detail: Any,
        response: httpx.Response | None = None,
    ) -> None:
        super().__init__(service, f"{service} service responded with {status_code}")
        self.status_code = status_code
        self.detail = detail
        self.response = response


class ServiceClient:
    """Base helper for HTTP micro-service clients."""

    service_name: str = "service"
    env_prefix: str | None = None
    default_base_url: str = "http://localhost"
    default_timeout: float = 10.0

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        default_headers: Mapping[str, str] | None = None,
    ) -> None:
        prefix = (self.env_prefix or self.service_name).upper()
        base_env = os.getenv(f"{prefix}_SERVICE_URL")
        timeout_env = os.getenv(f"{prefix}_SERVICE_TIMEOUT")

        resolved_base = (base_url or base_env or self.default_base_url or "").strip()
        self._base_url = resolved_base.rstrip("/") or self.default_base_url.rstrip("/")

        if timeout is not None:
            resolved_timeout = float(timeout)
        elif timeout_env:
            resolved_timeout = float(timeout_env)
        else:
            resolved_timeout = float(self.default_timeout)

        self._timeout = resolved_timeout
        self._transport = transport
        self._default_headers = dict(default_headers or {})

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def timeout(self) -> float:
        return self._timeout

    async def request(
        self,
        method: str,
        path: str,
        *,
        parse_json: bool = True,
        expected_status: Optional[int] = None,
        headers: Optional[Mapping[str, str]] = None,
        **kwargs: Any,
    ) -> Any:
        """Perform an HTTP request and return the parsed payload."""

        url_path = path.lstrip("/")
        request_headers: MutableMapping[str, str] = dict(self._default_headers)
        if headers:
            request_headers.update(headers)

        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                transport=self._transport,
            ) as client:
                response = await client.request(
                    method,
                    url_path,
                    headers=request_headers or None,
                    **kwargs,
                )
        except httpx.TimeoutException as exc:
            raise ServiceUnavailableError(
                self.service_name, f"{self.service_name} service request timed out"
            ) from exc
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError(
                self.service_name, f"{self.service_name} service request failed"
            ) from exc

        if expected_status is not None and response.status_code != expected_status:
            detail = self._extract_error_detail(response)
            raise ServiceResponseError(
                self.service_name,
                status_code=response.status_code,
                detail=detail,
                response=response,
            )

        if response.status_code >= 400:
            detail = self._extract_error_detail(response)
            raise ServiceResponseError(
                self.service_name,
                status_code=response.status_code,
                detail=detail,
                response=response,
            )

        if not parse_json:
            return response

        if not response.content:
            return None

        try:
            return response.json()
        except ValueError as exc:
            raise ServiceResponseError(
                self.service_name,
                status_code=response.status_code,
                detail="Invalid JSON payload",
                response=response,
            ) from exc

    def _extract_error_detail(self, response: httpx.Response) -> Any:
        try:
            payload = response.json()
        except ValueError:
            return response.text.strip() or response.reason_phrase

        if isinstance(payload, Mapping):
            for key in ("detail", "message", "error", "reason"):
                detail = payload.get(key)
                if detail:
                    return detail
        return payload


class AccountServiceClient(ServiceClient):
    service_name = "account"
    default_base_url = "http://localhost:8020"

    async def fetch_info(self, *, force_refresh: bool = False) -> Dict[str, Any] | None:
        payload = await self.request(
            "GET",
            "/account/info",
            params={"force_refresh": str(force_refresh).lower()},
        )
        if not isinstance(payload, Mapping):
            return None
        if "account_info" in payload and isinstance(payload["account_info"], Mapping):
            return dict(payload["account_info"])
        if "data" in payload and isinstance(payload["data"], Mapping):
            return dict(payload["data"])
        if "result" in payload and isinstance(payload["result"], Mapping):
            return dict(payload["result"])
        filtered = {
            key: value
            for key, value in payload.items()
            if key not in {"success", "from_cache"}
        }
        return dict(filtered) if filtered else None

    async def fetch_positions(
        self,
        *,
        fast: bool = False,
        force_refresh: bool = False,
    ) -> list[Any] | None:
        payload = await self.request(
            "GET",
            "/account/positions",
            params={
                "fast": str(fast).lower(),
                "force_refresh": str(force_refresh).lower(),
            },
        )
        if isinstance(payload, list):
            return list(payload)
        if isinstance(payload, Mapping):
            for key in ("positions", "data", "result"):
                value = payload.get(key)
                if isinstance(value, list):
                    return list(value)
        return None


class RiskServiceClient(ServiceClient):
    service_name = "risk"
    env_prefix = "RISK"
    default_base_url = "http://localhost:8030"

    async def fetch_rules(self) -> Any:
        return await self.request("GET", "/risk_rules")

    async def post_rule(self, payload: Mapping[str, Any]) -> Any:
        return await self.request("POST", "/risk_rules", json=dict(payload))

    async def update_rule(self, symbol: str, payload: Mapping[str, Any]) -> Any:
        return await self.request(
            "PUT",
            f"/risk_rules/{symbol}",
            json=dict(payload),
        )

    async def delete_rule(self, symbol: str) -> Any:
        return await self.request("DELETE", f"/risk_rules/{symbol}")

    async def fetch_config(self) -> Any:
        return await self.request("GET", "/risk-config")

    async def update_config(self, payload: Mapping[str, Any]) -> Any:
        return await self.request("PUT", "/risk-config", json=dict(payload))

    async def fetch_order_queue_status(self) -> Any:
        return await self.request("GET", "/order_queue_status")

    async def position_sizing(self, payload: Mapping[str, Any]) -> Any:
        return await self.request("POST", "/position-sizing", json=dict(payload))

    async def fetch_sizing_history(self, params: Mapping[str, Any]) -> Any:
        return await self.request("GET", "/sizing/history", params=dict(params))

    async def fetch_config_rules(self) -> Any:
        return await self.request("GET", "/config/rules")

    async def update_config_rules(self, payload: Mapping[str, Any]) -> Any:
        return await self.request("PUT", "/config/rules", json=dict(payload))


class StrategyServiceClient(ServiceClient):
    service_name = "strategy"
    env_prefix = "STRATEGY"
    default_base_url = "http://localhost:8045"

    async def list_strategies(self) -> Any:
        return await self.request("GET", "/strategy/list")

    async def fetch_strategy(self, name: str) -> Any:
        return await self.request("GET", f"/strategy/{name}")

    async def update_strategy(self, name: str, updates: Mapping[str, Any]) -> Any:
        return await self.request(
            "PUT",
            f"/strategy/{name}",
            json=dict(updates),
        )

    async def post_strategy(self, payload: Mapping[str, Any]) -> Any:
        return await self.request("POST", "/strategy", json=dict(payload))


class MarketDataServiceClient(ServiceClient):
    service_name = "market_data"
    env_prefix = "MARKET_DATA"
    default_base_url = "http://localhost:8010"

    async def fetch_current_price(self, symbol: str) -> Any:
        return await self.request(
            "GET",
            "/market/current_price",
            params={"symbol": symbol},
        )

    async def request_historical_data(self, payload: Mapping[str, Any]) -> Any:
        return await self.request(
            "POST",
            "/market/historical",
            json=dict(payload),
        )


class ConnectionControlClient(ServiceClient):
    service_name = "connection_control"
    env_prefix = "CONNECTION_CONTROL"
    default_base_url = "http://localhost:8050"

    async def fetch_config(self) -> Any:
        return await self.request("GET", "/connection/config")

    async def update_config(self, payload: Mapping[str, Any]) -> Any:
        return await self.request("PUT", "/connection/config", json=dict(payload))

    async def fetch_status(self) -> Any:
        return await self.request("GET", "/connection/status")

    async def connect(self, payload: Mapping[str, Any]) -> Any:
        return await self.request("POST", "/connection/connect", json=dict(payload))

    async def disconnect(self) -> Any:
        return await self.request("POST", "/connection/disconnect")

