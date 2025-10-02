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
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, MutableMapping, Optional

import httpx

__all__ = [
    "ServiceClient",
    "ServiceClientError",
    "ServiceUnavailableError",
    "ServiceResponseError",
    "AccountServiceClient",
    "AccountServiceResult",
    "OrdersServiceClient",
    "RiskServiceClient",
    "StrategyServiceClient",
    "MarketDataServiceClient",
    "NotificationServiceClient",
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


@dataclass(slots=True)
class AccountServiceResult:
    """Container holding payload data and accompanying metadata."""

    data: Any
    metadata: Dict[str, Any] = field(default_factory=dict)
    raw: Mapping[str, Any] | None = None

    def is_stale(self) -> bool:
        return AccountServiceClient.is_metadata_stale(self.metadata)

    def snapshot_updated_at(self) -> Optional[str]:
        value = self.metadata.get("snapshot_updated_at")
        if isinstance(value, str):
            return value
        updated_at = self.metadata.get("updated_at")
        return str(updated_at) if isinstance(updated_at, str) else None


class AccountServiceClient(ServiceClient):
    service_name = "account"
    default_base_url = "http://localhost:8020"

    _METADATA_KEYS = (
        "source",
        "snapshot_updated_at",
        "updated_at",
        "stale",
        "stale_reason",
        "stale_at",
        "from_cache",
    )

    @staticmethod
    def is_metadata_stale(metadata: Mapping[str, Any] | None) -> bool:
        if not isinstance(metadata, Mapping):
            return False
        stale = metadata.get("stale")
        if isinstance(stale, bool) and stale:
            return True
        reason = metadata.get("stale_reason")
        if isinstance(reason, str) and reason.strip():
            return True
        return False

    @staticmethod
    def _bool_param(value: bool) -> Optional[str]:
        return "true" if value else None

    @classmethod
    def _extract_metadata(cls, payload: Mapping[str, Any]) -> Dict[str, Any]:
        metadata: Dict[str, Any] = {}

        def _merge(source: Mapping[str, Any]) -> None:
            for key in cls._METADATA_KEYS:
                if key in metadata:
                    continue
                value = source.get(key)
                if value is not None:
                    metadata[key] = value

        _merge(payload)
        for key in ("metadata", "meta", "snapshot", "snapshot_metadata"):
            nested = payload.get(key)
            if isinstance(nested, Mapping):
                _merge(nested)

        return metadata

    @staticmethod
    def _extract_mapping_payload(
        payload: Mapping[str, Any],
        *,
        keys: tuple[str, ...],
    ) -> Any:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, Mapping):
                return dict(value)
            if isinstance(value, list):
                return list(value)
        filtered = {
            key: value
            for key, value in payload.items()
            if key not in {"success", "from_cache"}
        }
        return dict(filtered) if filtered else None

    def _wrap_result(
        self,
        payload: Any,
        *,
        data_keys: tuple[str, ...],
    ) -> AccountServiceResult | None:
        if isinstance(payload, Mapping):
            metadata = self._extract_metadata(payload)
            data = self._extract_mapping_payload(payload, keys=data_keys)
            if data is None:
                return None
            return AccountServiceResult(data=data, metadata=metadata, raw=dict(payload))

        if isinstance(payload, list):
            return AccountServiceResult(data=list(payload), metadata={}, raw=None)

        return None

    async def fetch_info(
        self,
        *,
        force_refresh: bool = False,
    ) -> AccountServiceResult | None:
        params: Dict[str, str] = {}
        if force_refresh:
            params["force_refresh"] = "true"

        payload = await self.request(
            "GET",
            "/account/info",
            params=params or None,
        )

        return self._wrap_result(
            payload,
            data_keys=("account_info", "data", "result"),
        )

    async def fetch_positions(
        self,
        *,
        fast: bool = False,
        force_refresh: bool = False,
    ) -> AccountServiceResult | None:
        params: Dict[str, str] = {}
        if fast:
            params["fast"] = "true"
        if force_refresh:
            params["force_refresh"] = "true"

        payload = await self.request(
            "GET",
            "/account/positions",
            params=params or None,
        )

        result = self._wrap_result(
            payload,
            data_keys=("positions", "data", "result"),
        )

        if result and isinstance(result.data, list):
            return result

        if isinstance(payload, list):
            return AccountServiceResult(data=list(payload), metadata={}, raw=None)

        return None

    async def fetch_orders(
        self,
        *,
        limit: int,
        offset: int,
        start_date: str | None = None,
    ) -> Mapping[str, Any] | list[Any] | None:
        params: Dict[str, Any] = {"limit": int(limit), "offset": int(offset)}
        if start_date:
            params["start_date"] = start_date

        return await self.request(
            "GET",
            "/account/orders",
            params=params,
        )

    async def request_refresh(self, *, reason: str | None = None) -> None:
        payload: Dict[str, Any] = {}
        if reason:
            payload["reason"] = reason

        await self.request(
            "POST",
            "/account/refresh",
            json=payload,
            expected_status=202,
            parse_json=False,
        )


class OrdersServiceClient(ServiceClient):
    """HTTP client for the Orders microservice."""

    service_name = "orders"
    env_prefix = "ORDERS"
    default_base_url = "http://localhost:8050"

    async def list_orders(
        self,
        *,
        limit: int | None = None,
        offset: int | None = None,
        start_date: str | None = None,
    ) -> Any:
        params: Dict[str, Any] = {}
        if limit is not None:
            params["limit"] = int(limit)
        if offset is not None:
            params["offset"] = int(offset)
        if start_date:
            params["start_date"] = start_date
        return await self.request("GET", "/orders", params=params or None)

    async def place_order(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return await self.request(
            "POST",
            "/orders",
            json=dict(payload),
            expected_status=202,
        )

    async def get_order(self, order_id: str) -> Any:
        return await self.request("GET", f"/orders/{order_id}")

    async def get_order_events(self, order_id: str) -> Any:
        return await self.request("GET", f"/orders/{order_id}/events")

    async def cancel_order(self, order_id: str) -> Mapping[str, Any]:
        return await self.request(
            "POST",
            f"/orders/{order_id}/cancel",
            json={},
            expected_status=202,
        )

    async def cancel_all(self) -> Mapping[str, Any]:
        return await self.request(
            "POST",
            "/orders/cancel_all",
            json={},
            expected_status=202,
        )

    async def close_position(self, symbol: str) -> Mapping[str, Any]:
        return await self.request(
            "POST",
            f"/positions/{symbol}/close",
            json={},
            expected_status=202,
        )

    async def reverse_position(self, symbol: str) -> Mapping[str, Any]:
        return await self.request(
            "POST",
            f"/positions/{symbol}/reverse",
            json={},
            expected_status=202,
        )

    async def update_order_fill_metrics(
        self, order_id: str, payload: Mapping[str, Any]
    ) -> Any:
        return await self.request(
            "PATCH",
            f"/orders/{order_id}/fills",
            json=dict(payload),
        )


class RiskServiceClient(ServiceClient):
    """HTTP client for the Risk microservice."""

    service_name = "risk"
    env_prefix = "RISK"
    default_base_url = "http://localhost:8030"

    async def list_rules(self) -> Any:
        """Return the latest snapshot of configured risk rules."""

        return await self.request("GET", "/risk/rules")

    async def fetch_rules(self) -> Any:
        """Backward compatible alias for :meth:`list_rules`."""

        return await self.list_rules()

    async def create_rule(self, payload: Mapping[str, Any]) -> Any:
        """Create a new risk rule."""

        return await self.request("POST", "/risk/rules", json=dict(payload))

    async def post_rule(self, payload: Mapping[str, Any]) -> Any:
        """Backward compatible alias for :meth:`create_rule`."""

        return await self.create_rule(payload)

    async def update_rule(
        self,
        symbol: str,
        payload: Mapping[str, Any] | None = None,
        *,
        enabled: bool | None = None,
    ) -> Any:
        """Update an existing risk rule identified by *symbol*."""

        params: dict[str, Any] = {}
        if enabled is not None:
            params["enabled"] = "true" if enabled else "false"
        body = dict(payload or {})
        return await self.request(
            "PUT",
            f"/risk/rules/{symbol}",
            json=body or None,
            params=params or None,
        )

    async def delete_rule(self, symbol: str) -> Any:
        """Delete the rule for *symbol*."""

        return await self.request("DELETE", f"/risk/rules/{symbol}")

    async def get_config(self) -> Any:
        """Retrieve the persisted risk configuration."""

        return await self.request("GET", "/risk/config")

    async def fetch_config(self) -> Any:
        """Backward compatible alias for :meth:`get_config`."""

        return await self.get_config()

    async def update_config(self, payload: Mapping[str, Any]) -> Any:
        """Apply updates to the risk configuration."""

        return await self.request("PUT", "/risk/config", json=dict(payload))

    async def fetch_order_queue_status(self) -> Any:
        """Fetch the current order queue approval status."""

        return await self.request("GET", "/order_queue_status")

    async def position_sizing(self, payload: Mapping[str, Any]) -> Any:
        """Calculate position sizing via the risk service."""

        return await self.request("POST", "/position-sizing", json=dict(payload))

    async def fetch_sizing_history(
        self,
        *,
        symbol: str | None = None,
        since: str | None = None,
        limit: int | None = None,
    ) -> Any:
        """Return historical sizing recommendations."""

        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol
        if since:
            params["since"] = since
        if limit is not None:
            params["limit"] = int(limit)
        return await self.request("GET", "/sizing/history", params=params or None)

    async def fetch_rules_history(
        self,
        *,
        symbol: str | None = None,
        limit: int | None = None,
    ) -> Any:
        """Return change history entries for configured rules."""

        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol
        if limit is not None:
            params["limit"] = int(limit)
        return await self.request("GET", "/risk/rules/history", params=params or None)

    async def fetch_config_rules(self) -> Any:
        """Retrieve the S-rule configuration payload."""

        return await self.request("GET", "/config/rules")

    async def update_config_rules(self, payload: Mapping[str, Any]) -> Any:
        """Update the S-rule configuration payload."""

        return await self.request("PUT", "/config/rules", json=dict(payload))


class StrategyServiceClient(ServiceClient):
    service_name = "strategy"
    env_prefix = "STRATEGY"
    default_base_url = "http://localhost:8045"

    async def get_settings(self) -> Any:
        return await self.request("GET", "/strategy/settings")

    async def update_settings(self, payload: Mapping[str, Any]) -> Any:
        return await self.request(
            "POST",
            "/strategy/settings",
            json=dict(payload),
        )

    async def generate_settings_token(self) -> Any:
        return await self.request("POST", "/strategy/settings/generate-token")

    async def list_files(self) -> Any:
        return await self.request("GET", "/strategy/files")

    async def list_strategies(self) -> Any:
        return await self.request("GET", "/strategies")

    async def create_strategy(self, payload: Mapping[str, Any]) -> Any:
        return await self.request("POST", "/strategies", json=dict(payload))

    async def get_strategy(self, name: str) -> Any:
        return await self.request("GET", f"/strategies/{name}")

    async def get_internal_info(self, name: str) -> Any:
        return await self.request("GET", f"/strategies/{name}/internal-info")

    async def update_internal_parameter(
        self, name: str, payload: Mapping[str, Any]
    ) -> Any:
        return await self.request(
            "POST",
            f"/strategies/{name}/internal-info/parameter",
            json=dict(payload),
        )

    async def get_risk_settings(self, name: str) -> Any:
        return await self.request("GET", f"/strategies/{name}/risk-settings")

    async def update_risk_settings(self, name: str, payload: Mapping[str, Any]) -> Any:
        return await self.request(
            "PUT",
            f"/strategies/{name}/risk-settings",
            json=dict(payload),
        )

    async def get_strategy_status(self, name: str) -> Any:
        return await self.request("GET", f"/strategies/{name}/status")

    async def request_data_push(self, name: str, payload: Mapping[str, Any]) -> Any:
        return await self.request(
            "POST",
            f"/strategies/{name}/data-push",
            json=dict(payload),
        )

    async def update_strategy(self, name: str, updates: Mapping[str, Any]) -> Any:
        return await self.request(
            "PUT",
            f"/strategies/{name}",
            json=dict(updates),
        )

    async def rename_strategy(self, name: str, payload: Mapping[str, Any]) -> Any:
        return await self.request(
            "POST",
            f"/strategies/{name}/rename",
            json=dict(payload),
        )

    async def delete_strategy(self, name: str) -> Any:
        return await self.request("DELETE", f"/strategies/{name}")

    async def toggle_strategy(self, name: str, *, enabled: bool) -> Any:
        return await self.request(
            "POST",
            f"/strategies/{name}/toggle",
            json={"enabled": bool(enabled)},
        )

    async def reset_strategy(self, name: str) -> Any:
        return await self.request("POST", f"/strategies/{name}/reset")

    async def get_strategy_performance(
        self,
        name: str,
        *,
        range_: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> Any:
        params: Dict[str, Any] = {}
        if range_ is not None:
            params["range"] = range_
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end
        return await self.request(
            "GET",
            f"/strategies/{name}/performance",
            params=params or None,
        )

    async def get_daily_pnl(self, name: str, *, force_refresh: bool = False) -> Any:
        params = {"force_refresh": str(force_refresh).lower()}
        return await self.request(
            "GET",
            f"/strategies/{name}/pnl/daily",
            params=params,
        )

    async def get_executions(
        self, name: str, *, limit: int = 100, force_refresh: bool = False
    ) -> Any:
        params = {
            "limit": max(1, min(int(limit or 1), 1000)),
            "force_refresh": str(force_refresh).lower(),
        }
        return await self.request(
            "GET",
            f"/strategies/{name}/executions",
            params=params,
        )

    async def clear_executions(self, name: str) -> Any:
        return await self.request("DELETE", f"/strategies/{name}/executions")

    async def submit_external_order(self, payload: Mapping[str, Any]) -> Any:
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

    async def request_chart_data(self, payload: Mapping[str, Any]) -> Any:
        return await self.request(
            "POST",
            "/chart_data",
            json=dict(payload),
        )


class NotificationServiceClient(ServiceClient):
    service_name = "notification"
    env_prefix = "NOTIFICATION"
    default_base_url = "http://localhost:8055"

    async def dispatch(self, payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
        return await self.request("POST", "/notifications/dispatch", json=dict(payload))

    async def test(self, payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
        return await self.request("POST", "/notifications/test", json=dict(payload))

    async def history(
        self,
        *,
        channel: str | None = None,
        status: str | None = None,
        since: str | None = None,
        limit: int | None = None,
    ) -> Mapping[str, Any]:
        params: dict[str, Any] = {}
        if channel:
            params["channel"] = channel
        if status:
            params["status"] = status
        if since:
            params["since"] = since
        if limit is not None:
            params["limit"] = int(limit)
        return await self.request("GET", "/notifications/history", params=params)

    async def clear_history(self) -> Mapping[str, Any]:
        return await self.request("DELETE", "/notifications/history")


class ConnectionControlClient(ServiceClient):
    service_name = "connection_control"
    env_prefix = "CONNECTION_CONTROL"
    default_base_url = "http://localhost:8056"

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

