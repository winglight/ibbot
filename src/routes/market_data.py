import asyncio
import contextlib
import math
import os
from datetime import timedelta
from typing import Dict, Any, Optional, Mapping, Iterable

import httpx

from fastapi import (
    APIRouter,
    HTTPException,
    Depends,
    WebSocket,
    WebSocketDisconnect,
    Query,
)
from loguru import logger

from pydantic import BaseModel, ConfigDict, Field

from .auth import get_current_user
from src.models import (
    HistoricalDataRequest,
    ChartDataRequest,
    CurrentPriceRequest,
)
from src.utils import now_local
from src.routes.service_clients import (
    MarketDataServiceClient,
    ServiceClientError,
    ServiceResponseError,
    ServiceUnavailableError,
)

router = APIRouter()

# Cache controls
CURRENT_PRICE_CACHE_ONLY = os.getenv("CURRENT_PRICE_CACHE_ONLY", "False").lower() == "true"
historical_data_cache: Dict[str, Dict[str, Any]] = {}
CACHE_DURATION_MINUTES = 1  # 缓存1分钟

_MARKET_DATA_SERVICE_CLIENT = MarketDataServiceClient()


def get_market_data_service_client() -> MarketDataServiceClient:
    """Return the shared market data service client instance.

    The legacy ``tests/routes`` suites monkeypatch this helper to inject
    lightweight doubles.  Routing dependencies continue to use
    :func:`_get_market_data_service_client`, which now delegates through the
    public accessor so that those monkeypatches take effect.
    """

    return _MARKET_DATA_SERVICE_CLIENT


class MarketBackfillRequest(BaseModel):
    symbol: str
    duration: str
    bar_size: str = Field(..., alias="barSize")
    exchange: str = "SMART"
    extra_bar_sizes: list[str] | None = Field(default=None, alias="extraBarSizes")
    extra_intervals: list[str] | None = Field(default=None, alias="extraIntervals")

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

def _get_market_data_service_client() -> MarketDataServiceClient:
    return get_market_data_service_client()


def _market_service_base_url(client: Optional[MarketDataServiceClient] = None) -> str:
    resolved = client.base_url if client else _MARKET_DATA_SERVICE_CLIENT.base_url
    return resolved or MarketDataServiceClient.default_base_url


async def _market_service_get(
    path: str,
    *,
    client: Optional[MarketDataServiceClient] = None,
) -> httpx.Response | Any:
    resolved = client or _get_market_data_service_client()
    request = getattr(resolved, "request")
    try:
        return await request("GET", path, parse_json=False)
    except TypeError as exc:
        message = str(exc)
        if "parse_json" not in message:
            raise
        return await request("GET", path)


_PRICE_FIELDS: tuple[str, ...] = (
    "open",
    "high",
    "low",
    "close",
    "last",
    "bid",
    "ask",
    "mid",
    "mid_price",
    "midPrice",
    "previous_close",
    "previousClose",
    "mark_price",
    "markPrice",
)


def _is_effective_scale(value: Any) -> bool:
    try:
        scale = float(value)
    except (TypeError, ValueError):
        return False
    if scale == 0 or not math.isfinite(scale):
        return False
    return abs(scale - 1.0) > 1e-9


def _normalise_price_scale(mapping: Mapping[str, Any]) -> Dict[str, Any]:
    """Return *mapping* with price fields restored to raw values if scaled."""

    result: Dict[str, Any] = dict(mapping)
    scale_value = result.get("price_scale")
    if _is_effective_scale(scale_value):
        scale = float(scale_value)
        factor = 1.0 / scale
        for field in _PRICE_FIELDS:
            if field in result:
                value = result[field]
                if isinstance(value, (int, float)) and math.isfinite(value):
                    result[field] = float(value) * factor
        # Clean up nested metadata that may also carry scale hints.
        metadata = result.get("metadata")
        if isinstance(metadata, Mapping):
            result["metadata"] = _normalise_price_scale(metadata)
        result.pop("price_scale", None)
        result.pop("price_scale_source", None)
    else:
        metadata = result.get("metadata")
        if isinstance(metadata, Mapping):
            result["metadata"] = _normalise_price_scale(metadata)
    return result


def _normalise_price_scaled_series(items: Iterable[Any]) -> list[Any]:
    normalised: list[Any] = []
    for item in items:
        if isinstance(item, Mapping):
            normalised.append(_normalise_price_scale(item))
        else:
            normalised.append(item)
    return normalised


def _clean_expired_cache() -> None:
    """清理过期的内存缓存"""
    global historical_data_cache
    now = now_local()
    expired_keys = [
        key
        for key, value in historical_data_cache.items()
        if value.get("expires_at", now) <= now
    ]
    for key in expired_keys:
        del historical_data_cache[key]
    if expired_keys:
        logger.debug(f"Cleaned {len(expired_keys)} expired cache entries")


@router.post("/api/historical_data")
async def get_historical_data(
    request: HistoricalDataRequest,
    user: Optional[str] = Depends(get_current_user),
):
    """获取历史数据"""
    try:
        _clean_expired_cache()
        cache_key = f"{request.symbol}_{request.duration}_{request.bar_size}_{request.exchange}"
        now = now_local()

        cache_entry = historical_data_cache.get(cache_key)
        if cache_entry and cache_entry.get("expires_at", now) > now:
            logger.debug(f"Using memory cache for {request.symbol}")
            cached_payload = cache_entry.get("payload")
            if isinstance(cached_payload, Mapping):
                response_payload = dict(cached_payload)
                response_payload.setdefault(
                    "source", cache_entry.get("source", "memory_cache")
                )
                response_payload.setdefault("success", True)
                return response_payload

        client = _get_market_data_service_client()
        service_payload = request.model_dump(exclude_none=True)
        alias_payload = request.model_dump(by_alias=True, exclude_none=True)
        service_payload.update(alias_payload)

        try:
            service_result = await client.request_historical_data(service_payload)
        except ServiceUnavailableError as exc:
            raise HTTPException(
                status_code=503, detail="Market data service unavailable"
            ) from exc
        except ServiceResponseError as exc:
            if 500 <= exc.status_code < 600:
                raise HTTPException(
                    status_code=502, detail="Market data service error"
                ) from exc
            raise HTTPException(
                status_code=exc.status_code,
                detail=exc.detail or "Market data service error",
            ) from exc
        except ServiceClientError as exc:
            logger.error(
                "Market data service request failed for {}: {}",
                request.symbol,
                exc,
            )
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        if not isinstance(service_result, Mapping):
            raise HTTPException(
                status_code=502, detail="Unexpected response from market data service"
            )

        response_payload = dict(service_result)
        cached_data = response_payload.get("data")
        if isinstance(cached_data, list):
            normalised = _normalise_price_scaled_series(cached_data)
            response_payload["data"] = normalised
            historical_data_cache[cache_key] = {
                "payload": dict(response_payload),
                "timestamp": now,
                "expires_at": now + timedelta(minutes=CACHE_DURATION_MINUTES),
                "source": response_payload.get("source", "market_data_service"),
            }
        elif isinstance(cached_data, Mapping):
            normalised = _normalise_price_scale(cached_data)
            response_payload["data"] = normalised
            historical_data_cache[cache_key] = {
                "payload": dict(response_payload),
                "timestamp": now,
                "expires_at": now + timedelta(minutes=CACHE_DURATION_MINUTES),
                "source": response_payload.get("source", "market_data_service"),
            }

        response_payload.setdefault("success", bool(cached_data))
        response_payload.setdefault("rows", len(cached_data) if isinstance(cached_data, list) else 0)
        response_payload.setdefault("source", "market_data_service")
        response_payload.setdefault("filename", None)
        response_payload.setdefault("symbol", request.symbol)
        response_payload.setdefault("duration", request.duration)
        response_payload.setdefault("bar_size", request.bar_size)
        response_payload.setdefault("exchange", request.exchange)
        return response_payload

    except HTTPException as exc:
        # Preserve explicit HTTP errors (e.g. 404/408) so callers can handle
        # them without the response being wrapped into a 500.
        raise exc
    except Exception as e:
        logger.error(f"Historical data error: {e}")
        raise HTTPException(status_code=500, detail=str(e))



async def _fetch_chart_data_payload(request: ChartDataRequest) -> Dict[str, Any]:
    _clean_expired_cache()
    now = now_local()
    use_cache = bool(getattr(request, "use_cache", True))
    force_refresh = bool(getattr(request, "force_refresh", False))

    cache_key_base = f"{request.symbol}_{request.duration}_{request.bar_size}_{request.exchange}"
    cache_key = f"chart::{cache_key_base}"

    if use_cache and not force_refresh:
        cache_entry = historical_data_cache.get(cache_key)
        if cache_entry and cache_entry.get("expires_at", now) > now:
            logger.debug(f"Using chart memory cache for {request.symbol}")
            cached_payload = cache_entry.get("payload")
            if isinstance(cached_payload, Mapping):
                response_payload = dict(cached_payload)
                response_payload.setdefault("success", True)
                return response_payload

    client = _get_market_data_service_client()
    service_payload = request.model_dump(exclude_none=True)
    alias_payload = request.model_dump(by_alias=True, exclude_none=True)
    service_payload.update(alias_payload)

    force_requested = bool(service_payload.get("force_refresh") or service_payload.get("forceRefresh"))

    async def _request_chart(payload: Dict[str, Any]) -> Mapping[str, Any]:
        try:
            return await client.request_chart_data(payload)
        except ServiceUnavailableError as exc:
            raise HTTPException(status_code=503, detail="Market data service unavailable") from exc
        except ServiceResponseError as exc:
            if exc.status_code == 404:
                raise HTTPException(status_code=404, detail="No chart data available") from exc
            if 500 <= exc.status_code < 600:
                raise HTTPException(status_code=502, detail="Market data service error") from exc
            raise HTTPException(
                status_code=exc.status_code,
                detail=exc.detail or "Market data service error",
            ) from exc
        except ServiceClientError as exc:
            logger.error(
                "Market data chart data request failed for {}: {}", request.symbol, exc
            )
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    try:
        service_result = await _request_chart(service_payload)
    except HTTPException as exc:
        if exc.status_code == 404 and not force_requested:
            fallback_payload = dict(service_payload)
            fallback_payload["use_cache"] = False
            fallback_payload["useCache"] = False
            fallback_payload["force_refresh"] = True
            fallback_payload["forceRefresh"] = True
            service_result = await _request_chart(fallback_payload)
        else:
            raise

    if not isinstance(service_result, Mapping):
        raise HTTPException(status_code=502, detail="Unexpected response from market data service")

    response_payload = dict(service_result)
    data = response_payload.get("data")
    if isinstance(data, list):
        response_payload["data"] = _normalise_price_scaled_series(data)
        data = response_payload["data"]
    elif isinstance(data, Mapping):
        response_payload["data"] = _normalise_price_scale(data)
        data = response_payload["data"]

    if not data:
        raise HTTPException(status_code=404, detail="No chart data available")

    if use_cache:
        historical_data_cache[cache_key] = {
            "payload": dict(response_payload),
            "timestamp": now,
            "expires_at": now + timedelta(minutes=CACHE_DURATION_MINUTES),
            "source": response_payload.get("source", "market_data_service"),
        }

    response_payload.setdefault("success", True)
    response_payload.setdefault("symbol", request.symbol)
    response_payload.setdefault("duration", request.duration)
    response_payload.setdefault("bar_size", request.bar_size)
    response_payload.setdefault("source", "market_data_service")
    return response_payload


@router.post("/api/chart_data")
async def get_chart_data(
    request: ChartDataRequest,
    user: Optional[str] = Depends(get_current_user),
) -> Dict[str, Any]:
    """获取图表数据 (API namespace)."""
    return await _fetch_chart_data_payload(request)


@router.post("/chart_data")
async def get_chart_data_legacy(
    request: ChartDataRequest,
    user: Optional[str] = Depends(get_current_user),
) -> Dict[str, Any]:
    """向后兼容的图表数据接口，路径不带 /api 前缀。"""
    return await _fetch_chart_data_payload(request)


async def _execute_market_backfill(request: MarketBackfillRequest) -> Dict[str, Any]:
    client = _get_market_data_service_client()
    service_payload = request.model_dump(by_alias=True, exclude_none=True)

    try:
        result = await client.request_backfill(service_payload)
    except ServiceUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Market data service unavailable") from exc
    except ServiceResponseError as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=404, detail="No market data available") from exc
        if 500 <= exc.status_code < 600:
            raise HTTPException(status_code=502, detail="Market data service error") from exc
        raise HTTPException(status_code=exc.status_code, detail=exc.detail or "Market data service error") from exc
    except ServiceClientError as exc:
        logger.error("Market data backfill request failed for {}: {}", request.symbol, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if not isinstance(result, Mapping):
        return {"success": True, "result": result}

    payload = dict(result)
    payload.setdefault("success", bool(payload.get("rows")))
    payload.setdefault("symbol", request.symbol)
    payload.setdefault("interval", payload.get("interval") or request.bar_size)
    return payload


@router.post("/market/backfill")
async def run_market_backfill(
    request: MarketBackfillRequest,
    user: Optional[str] = Depends(get_current_user),
) -> Dict[str, Any]:
    return await _execute_market_backfill(request)


@router.post("/api/market/backfill")
async def run_market_backfill_api(
    request: MarketBackfillRequest,
    user: Optional[str] = Depends(get_current_user),
) -> Dict[str, Any]:
    """API-prefixed alias for :func:`run_market_backfill`.

    The frontend consistently calls ``/api``-prefixed endpoints via
    :meth:`static/js/app.js:IBApp.apiCall`, so exposing this alias
    prevents 404 responses when the configuration sidebar triggers a
    backfill.
    """

    return await _execute_market_backfill(request)


@router.get("/market/storage")
async def proxy_market_storage(
    symbol: str = Query(..., description="Ticker symbol"),
    include_parquet: bool = Query(True, alias="includeParquet"),
    user: Optional[str] = Depends(get_current_user),
) -> Dict[str, Any]:
    symbol_clean = (symbol or "").strip().upper()
    if not symbol_clean:
        raise HTTPException(status_code=400, detail="symbol is required")

    client = _get_market_data_service_client()
    try:
        result = await client.request_market_storage(
            symbol=symbol_clean,
            include_parquet=include_parquet,
        )
    except ServiceUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Market data service unavailable") from exc
    except ServiceResponseError as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=404, detail="Symbol not found") from exc
        if 500 <= exc.status_code < 600:
            raise HTTPException(status_code=502, detail="Market data service error") from exc
        raise HTTPException(status_code=exc.status_code, detail=exc.detail or "Market data service error") from exc
    except ServiceClientError as exc:
        logger.error("Market data storage request failed for {}: {}", symbol_clean, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if isinstance(result, Mapping):
        payload = dict(result)
        payload.setdefault("symbol", symbol_clean)
        return payload

    return {"symbol": symbol_clean, "result": result}


@router.get("/api/realtime_data/{symbol}")
async def get_realtime_data(
    symbol: str,
    exchange: str = "SMART",
    user: Optional[str] = Depends(get_current_user),
    client: MarketDataServiceClient = Depends(_get_market_data_service_client),
):
    """获取实时数据"""
    try:
        try:
            result = await _market_service_get(f"/market/{symbol}", client=client)
        except TypeError as exc:
            if "client" not in str(exc):
                raise
            result = await _market_service_get(f"/market/{symbol}")
    except ServiceUnavailableError:
        raise HTTPException(status_code=503, detail="Market data service unavailable")
    except ServiceResponseError as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=404, detail="No data available")
        if 500 <= exc.status_code < 600:
            raise HTTPException(status_code=502, detail="Market data service error")
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail or "Market data service error",
        )
    except ServiceClientError as exc:
        logger.error("Market data realtime request failed for {}: {}", symbol, exc)
        raise HTTPException(status_code=502, detail=str(exc))

    if isinstance(result, httpx.Response):
        if result.status_code >= 400:
            raise HTTPException(status_code=502, detail="Market data service error")
        try:
            payload = result.json()
        except ValueError as exc:  # pragma: no cover - defensive guard
            raise HTTPException(status_code=502, detail="Unexpected market data format") from exc
    else:
        payload = result

    if not isinstance(payload, Mapping):
        raise HTTPException(status_code=502, detail="Unexpected market data format")

    return {"success": True, "data": dict(payload)}


@router.post("/api/current_price")
async def get_current_price(
    request: CurrentPriceRequest,
    user: Optional[str] = Depends(get_current_user),
    client: MarketDataServiceClient = Depends(_get_market_data_service_client),
) -> Dict[str, Any]:
    """获取当前价格 - 轻量级接口"""
    if CURRENT_PRICE_CACHE_ONLY:
        return {
            "success": False,
            "symbol": request.symbol,
            "price": None,
            "timestamp": now_local().isoformat(),
            "error": "Cache-only mode not supported without market data service",
        }

    try:
        payload = await client.fetch_current_price(request.symbol)
    except ServiceUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Market data service unavailable") from exc
    except ServiceResponseError as exc:
        if 500 <= exc.status_code < 600:
            raise HTTPException(status_code=502, detail="Market data service error") from exc
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail or "Market data service error",
        ) from exc
    except ServiceClientError as exc:
        logger.error(
            "Market data current price request failed for {}: {}", request.symbol, exc
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if isinstance(payload, Mapping):
        response: Dict[str, Any] = dict(payload)
    else:
        response = {"price": payload}

    response.setdefault("symbol", request.symbol)
    response.setdefault("timestamp", now_local().isoformat())
    response.setdefault("source", "market_data_service")

    price = response.get("price")
    if price is None:
        response.setdefault("success", False)
        response.setdefault("error", "Unable to get current price")
    else:
        response.setdefault("success", True)

    return response

class _MarketDataWebsocketSession:
    """Manage a single market data websocket connection."""

    def __init__(
        self,
        websocket: WebSocket,
        *,
        client: Optional[MarketDataServiceClient] = None,
        poll_interval: float = 1.0,
    ) -> None:
        self._websocket = websocket
        self._client = client or _get_market_data_service_client()
        self._interval = max(poll_interval, 0.2)
        self._symbols: set[str] = set()
        self._poll_task: asyncio.Task | None = None
        self._closed = asyncio.Event()
        self._send_lock = asyncio.Lock()

    async def start(self) -> None:
        await self._websocket.accept()
        self._poll_task = asyncio.create_task(self._poll_loop())
        try:
            while True:
                try:
                    message = await self._websocket.receive_text()
                except WebSocketDisconnect:
                    break
                await self._handle_message(message)
        finally:
            await self.close()

    async def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        if self._poll_task is not None:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
        with contextlib.suppress(Exception):
            await self._websocket.close()

    async def _handle_message(self, raw_message: str) -> None:
        try:
            payload = json.loads(raw_message)
        except ValueError:
            await self._send_error("invalid_payload", "Message must be JSON encoded")
            return

        message_type = (
            str(payload.get("type") or payload.get("action") or "")
            .strip()
            .lower()
        )

        if message_type in {"subscribe", "subscribe_quotes"}:
            await self._handle_subscribe(payload)
        elif message_type in {"unsubscribe", "unsubscribe_quotes"}:
            await self._handle_unsubscribe(payload)
        elif message_type in {"set_interval", "interval"}:
            await self._handle_set_interval(payload)
        elif message_type == "ping":
            await self._safe_send({"type": "pong", "source": "market_ws"})
        else:
            await self._send_error("unsupported", f"Unsupported message type: {message_type or 'unknown'}")

    async def _handle_subscribe(self, payload: Mapping[str, Any]) -> None:
        raw_symbols = payload.get("symbols")
        if isinstance(raw_symbols, str):
            symbols = [raw_symbols]
        elif isinstance(raw_symbols, (list, tuple, set)):
            symbols = list(raw_symbols)
        else:
            symbol = payload.get("symbol")
            symbols = [symbol] if isinstance(symbol, str) else []

        cleaned = {
            symbol.strip().upper()
            for symbol in symbols
            if isinstance(symbol, str) and symbol.strip()
        }

        if not cleaned:
            await self._send_error("invalid_subscription", "symbols is required")
            return

        new_symbols = cleaned - self._symbols
        if not new_symbols:
            await self._safe_send(
                {
                    "type": "subscription_ack",
                    "action": "subscribe",
                    "symbols": sorted(self._symbols),
                    "success": True,
                    "source": "market_ws",
                    "message": "Already subscribed",
                }
            )
            return

        self._symbols.update(new_symbols)
        await self._safe_send(
            {
                "type": "subscription_ack",
                "action": "subscribe",
                "symbols": sorted(new_symbols),
                "success": True,
                "source": "market_ws",
            }
        )
        asyncio.create_task(self._send_snapshot())

    async def _handle_unsubscribe(self, payload: Mapping[str, Any]) -> None:
        raw_symbols = payload.get("symbols")
        if isinstance(raw_symbols, str):
            symbols = [raw_symbols]
        elif isinstance(raw_symbols, (list, tuple, set)):
            symbols = list(raw_symbols)
        else:
            symbol = payload.get("symbol")
            symbols = [symbol] if isinstance(symbol, str) else []

        cleaned = {
            symbol.strip().upper()
            for symbol in symbols
            if isinstance(symbol, str) and symbol.strip()
        }
        removed = cleaned & self._symbols
        if removed:
            self._symbols.difference_update(removed)

        await self._safe_send(
            {
                "type": "subscription_ack",
                "action": "unsubscribe",
                "symbols": sorted(removed or cleaned),
                "success": True,
                "source": "market_ws",
            }
        )

    async def _handle_set_interval(self, payload: Mapping[str, Any]) -> None:
        interval_ms = payload.get("interval_ms")
        if interval_ms is None:
            interval_ms = payload.get("intervalMs")
        if interval_ms is None:
            interval_ms = payload.get("interval")

        try:
            interval_value = float(interval_ms) / 1000.0
        except (TypeError, ValueError):
            await self._send_error("invalid_interval", "interval_ms must be numeric")
            return

        self._interval = max(interval_value, 0.2)
        await self._safe_send(
            {
                "type": "interval_ack",
                "interval_ms": int(self._interval * 1000),
                "success": True,
                "source": "market_ws",
            }
        )

    async def _poll_loop(self) -> None:
        try:
            while not self._closed.is_set():
                try:
                    await asyncio.wait_for(
                        self._closed.wait(), timeout=self._interval
                    )
                except asyncio.TimeoutError:
                    if self._closed.is_set():
                        break
                    await self._send_snapshot()
        except asyncio.CancelledError:
            pass

    async def _send_snapshot(self) -> None:
        if not self._symbols or self._closed.is_set():
            return

        symbols = sorted(self._symbols)
        quotes: Dict[str, Any] = {}
        errors: list[str] = []

        tasks = [self._client.fetch_current_price(symbol) for symbol in symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for symbol, result in zip(symbols, results):
            if isinstance(result, Exception):
                logger.debug(
                    "Failed to fetch market price", symbol=symbol, error=str(result)
                )
                errors.append(symbol)
                continue

            if isinstance(result, Mapping):
                quotes[symbol] = self._normalize_quote(symbol, result)
            else:
                quotes[symbol] = {
                    "symbol": symbol,
                    "last": result,
                    "timestamp": now_local().isoformat(),
                    "source": "market_data_service",
                }

        if not quotes and errors:
            await self._send_error(
                "fetch_failed",
                f"Failed to load quotes for: {', '.join(sorted(errors))}",
            )
            return

        payload: Dict[str, Any] = {
            "type": "market_quotes",
            "source": "market_ws",
            "symbols": symbols,
            "quotes": quotes,
            "timestamp": now_local().isoformat(),
        }
        if errors:
            payload["partial"] = True
            payload["errors"] = sorted(errors)

        await self._safe_send(payload)

    async def _safe_send(self, payload: Mapping[str, Any]) -> None:
        if self._closed.is_set():
            return
        async with self._send_lock:
            try:
                await self._websocket.send_text(json.dumps(payload))
            except WebSocketDisconnect:
                await self.close()

    async def _send_error(self, code: str, detail: str) -> None:
        await self._safe_send(
            {
                "type": "error",
                "code": code,
                "detail": detail,
                "source": "market_ws",
            }
        )

    @staticmethod
    def _normalize_quote(symbol: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
        data = dict(payload)
        data.setdefault("symbol", symbol)
        if "price" in data and "last" not in data:
            data["last"] = data.get("price")
        if "timestamp" not in data:
            data["timestamp"] = now_local().isoformat()
        data.setdefault("source", "market_data_service")
        return data


@router.websocket("/ws/market")
async def proxy_market_data_stream(websocket: WebSocket) -> None:
    """Stream market data updates to UI clients using a single websocket."""

    session = _MarketDataWebsocketSession(websocket)
    await session.start()
