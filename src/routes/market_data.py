import asyncio
import contextlib
import os
from datetime import timedelta
from typing import Dict, Any, Optional, Mapping

import httpx
import websockets

from fastapi import APIRouter, HTTPException, Depends, WebSocket, WebSocketDisconnect, Query
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
MARKET_DATA_SERVICE_WS_URL = os.getenv("MARKET_DATA_SERVICE_WS_URL")
historical_data_cache: Dict[str, Dict[str, Any]] = {}
CACHE_DURATION_MINUTES = 1  # 缓存1分钟

_MARKET_DATA_SERVICE_CLIENT = MarketDataServiceClient()


class MarketBackfillRequest(BaseModel):
    symbol: str
    duration: str
    bar_size: str = Field(..., alias="barSize")
    exchange: str = "SMART"
    extra_bar_sizes: list[str] | None = Field(default=None, alias="extraBarSizes")
    extra_intervals: list[str] | None = Field(default=None, alias="extraIntervals")

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

def _get_market_data_service_client() -> MarketDataServiceClient:
    return _MARKET_DATA_SERVICE_CLIENT


def _market_service_base_url(client: Optional[MarketDataServiceClient] = None) -> str:
    resolved = client.base_url if client else _MARKET_DATA_SERVICE_CLIENT.base_url
    return resolved or MarketDataServiceClient.default_base_url


def _market_service_ws_url(client: Optional[MarketDataServiceClient] = None) -> str:
    if MARKET_DATA_SERVICE_WS_URL:
        return MARKET_DATA_SERVICE_WS_URL.rstrip("/")
    base = _market_service_base_url(client)
    if "://" not in base:
        base = f"http://{base}"
    scheme, rest = base.split("://", 1)
    ws_scheme = "wss" if scheme == "https" else "ws"
    return f"{ws_scheme}://{rest.rstrip('/')}/ws/market"


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
            logger.error("Market data chart data request failed for {}: {}", request.symbol, exc)
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


@router.post("/market/backfill")
async def run_market_backfill(
    request: MarketBackfillRequest,
    user: Optional[str] = Depends(get_current_user),
) -> Dict[str, Any]:
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
        logger.error("Market data current price request failed for {}: {}", request.symbol, exc)
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

@router.websocket("/ws/market")
async def proxy_market_data_stream(websocket: WebSocket) -> None:
    """Proxy websocket updates from the market data service to UI clients."""

    client = _get_market_data_service_client()
    upstream_url = _market_service_ws_url(client)
    await websocket.accept()

    async def _client_to_upstream(upstream: websockets.WebSocketClientProtocol) -> None:
        try:
            while True:
                message = await websocket.receive_text()
                await upstream.send(message)
        except WebSocketDisconnect:
            await upstream.close()
        except Exception:
            await upstream.close()

    async def _upstream_to_client(upstream: websockets.WebSocketClientProtocol) -> None:
        try:
            async for message in upstream:
                if isinstance(message, bytes):
                    await websocket.send_bytes(message)
                else:
                    await websocket.send_text(message)
        except websockets.ConnectionClosed:
            raise WebSocketDisconnect()

    try:
        async with websockets.connect(upstream_url, ping_interval=None) as upstream:
            forward_tasks = {
                asyncio.create_task(_client_to_upstream(upstream)),
                asyncio.create_task(_upstream_to_client(upstream)),
            }
            done, pending = await asyncio.wait(forward_tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in pending:
                task.cancel()
            for task in done:
                with contextlib.suppress(Exception):
                    task.result()
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.error("Market data websocket proxy error: {}", exc)
        with contextlib.suppress(Exception):
            await websocket.send_json(
                {"type": "error", "detail": "Market data service unavailable"}
            )
        with contextlib.suppress(Exception):
            await websocket.close(code=1011, reason="Upstream connection failed")
    finally:
        with contextlib.suppress(Exception):
            await websocket.close()
