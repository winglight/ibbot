import asyncio
import contextlib
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, Any, Optional, List, Mapping

import httpx
import websockets

import pandas as pd
from fastapi import APIRouter, HTTPException, Depends, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from loguru import logger

from src.database import get_db_manager
from .auth import get_current_user
from src.models import (
    HistoricalDataRequest,
    ChartDataRequest,
    CurrentPriceRequest,
)
from src.utils import now_local, to_local_iso, to_local_time
from src.data_layer import (
    DataSubscriptionRequest,
    DataSourceError,
    get_data_layer_mode,
    get_data_source_manager,
    is_legacy_mode,
)
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
NY_TZ = ZoneInfo("America/New_York")

_data_layer_manager = None

_MARKET_DATA_SERVICE_CLIENT = MarketDataServiceClient()

_BAR_SIZE_TO_INTERVAL = {
    "1 sec": "1s",
    "1 secs": "1s",
    "1 second": "1s",
    "1 seconds": "1s",
    "5 sec": "5s",
    "5 secs": "5s",
    "5 second": "5s",
    "5 seconds": "5s",
    "1 min": "1m",
    "1 mins": "1m",
    "1 minute": "1m",
    "1 minutes": "1m",
    "5 min": "5m",
    "5 mins": "5m",
    "5 minute": "5m",
    "5 minutes": "5m",
}


def _get_data_layer_manager():
    global _data_layer_manager
    if _data_layer_manager is not None:
        return _data_layer_manager
    try:
        _data_layer_manager = get_data_source_manager()
        return _data_layer_manager
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Failed to initialise data source manager: {}", exc)
        return None


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


def _map_bar_size_to_interval(bar_size: str) -> Optional[str]:
    if not bar_size:
        return None
    key = bar_size.strip().lower()
    return _BAR_SIZE_TO_INTERVAL.get(key)


def _filter_candles_by_duration(
    candles: List[Dict[str, Any]], duration_minutes: int
) -> List[Dict[str, Any]]:
    if duration_minutes <= 0:
        return candles
    cutoff = now_local() - timedelta(minutes=duration_minutes)
    filtered: List[Dict[str, Any]] = []
    for candle in candles:
        ts_ns = candle.get("interval_end_ns") or candle.get("interval_start_ns")
        if ts_ns is None:
            filtered.append(candle)
            continue
        try:
            ts = datetime.fromtimestamp(int(ts_ns) / 1_000_000_000, tz=NY_TZ)
        except Exception:
            filtered.append(candle)
            continue
        if ts >= cutoff:
            filtered.append(candle)
    return filtered


def _candles_to_records(candles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for candle in candles:
        ts_ns = candle.get("interval_end_ns") or candle.get("interval_start_ns")
        if ts_ns is not None:
            try:
                ts = datetime.fromtimestamp(int(ts_ns) / 1_000_000_000, tz=NY_TZ)
            except Exception:
                ts = now_local()
        else:
            ts = now_local()
        record = {
            "date": to_local_iso(ts),
            "open": candle.get("open"),
            "high": candle.get("high"),
            "low": candle.get("low"),
            "close": candle.get("close"),
            "volume": candle.get("volume"),
        }
        records.append(record)
    return records


def _parse_duration_to_minutes(duration: str) -> int:
    """Parse IB-style duration strings into minutes."""
    if not duration:
        return 0
    s = str(duration).strip().lower()
    parts = s.split()
    if not parts:
        return 0
    try:
        n = float(parts[0])
    except Exception:
        import re as _re
        m = _re.match(r"(\d+(?:\.\d+)?)([a-zA-Z]+)", s)
        if m:
            n = float(m.group(1))
            unit = m.group(2)
        else:
            return 0
    else:
        unit = parts[1] if len(parts) > 1 else "min"
    unit = unit.strip().lower()
    if unit in ("d", "day", "days"):
        return int(n * 1440)
    if unit in ("h", "hour", "hours"):
        return int(n * 60)
    if unit in ("w", "week", "weeks"):
        return int(n * 7 * 1440)
    if unit in ("m", "min", "mins", "minute", "minutes"):
        return int(n)
    return int(n)


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
    mode = get_data_layer_mode()
    manager = None if is_legacy_mode(mode) else _get_data_layer_manager()

    try:
        _clean_expired_cache()
        cache_key = f"{request.symbol}_{request.duration}_{request.bar_size}_{request.exchange}"
        now = now_local()

        if cache_key in historical_data_cache:
            cache_entry = historical_data_cache[cache_key]
            if cache_entry.get("expires_at", now) > now:
                logger.debug(f"Using memory cache for {request.symbol}")
                cached_data = cache_entry["data"]
                return {
                    "success": True,
                    "data": cached_data,
                    "filename": f"data/{request.symbol}_historical.csv",
                    "rows": len(cached_data),
                    "source": "memory_cache",
                }

        db_manager = get_db_manager()
        interval = db_manager._normalize_interval(request.bar_size)
        minutes = _parse_duration_to_minutes(request.duration)
        start_dt = now - timedelta(minutes=minutes)

        cache_df = db_manager.query_historical_cache(
            request.symbol,
            interval=interval,
            start_dt=start_dt.isoformat(timespec="seconds"),
            end_dt=now.isoformat(timespec="seconds"),
        )

        if not db_manager.detect_missing_ranges(
            cache_df, start_dt.isoformat(), now.isoformat(), interval
        ):
            logger.debug(f"Using database cache for {request.symbol}")
            data_dict = cache_df.to_dict("records")
            expires_at = now + timedelta(minutes=CACHE_DURATION_MINUTES)
            historical_data_cache[cache_key] = {
                "data": data_dict,
                "timestamp": now,
                "expires_at": expires_at,
            }
            filename = f"data/{request.symbol}_historical.csv"
            try:
                os.makedirs(os.path.dirname(filename), exist_ok=True)
                cache_df.to_csv(filename, index=False)
            except Exception as e:
                logger.warning(
                    f"Failed to write CSV backup for historical data (cache): {e}"
                )
            return {
                "success": True,
                "data": data_dict,
                "filename": filename,
                "rows": len(cache_df),
                "source": "database_cache",
            }

        # Unified data layer attempt ----------------------------------
        interval_key = _map_bar_size_to_interval(request.bar_size)
        duration_minutes = _parse_duration_to_minutes(request.duration)
        if manager and interval_key:
            try:
                candles = await manager.history(
                    DataSubscriptionRequest(
                        channel="market.tick",
                        symbol=(request.symbol or "").upper(),
                        interval=interval_key,
                        options={"exchange": request.exchange},
                    )
                )
            except DataSourceError as exc:
                logger.debug(
                    "Unified history unavailable for {}: {}",
                    request.symbol,
                    exc,
                )
                candles = []
            except Exception as exc:
                logger.exception(
                    "Unified history error for {}: {}",
                    request.symbol,
                    exc,
                )
                candles = []

            if candles:
                candles = _filter_candles_by_duration(candles, duration_minutes)
                records = _candles_to_records(candles)
                if records:
                    historical_data_cache[cache_key] = {
                        "data": records,
                        "timestamp": now,
                        "expires_at": now + timedelta(minutes=CACHE_DURATION_MINUTES),
                    }
                    return {
                        "success": True,
                        "data": records,
                        "filename": None,
                        "rows": len(records),
                        "source": "data_layer",
                    }

        client = _get_market_data_service_client()
        service_payload = request.model_dump(exclude_none=True)
        alias_payload = request.model_dump(by_alias=True, exclude_none=True)
        service_payload.update(alias_payload)

        try:
            service_result = await client.request_historical_data(service_payload)
        except ServiceUnavailableError:
            raise HTTPException(
                status_code=503, detail="Market data service unavailable"
            )
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

        records = service_result.get("data")
        if isinstance(records, list):
            expires_at = now + timedelta(minutes=CACHE_DURATION_MINUTES)
            historical_data_cache[cache_key] = {
                "data": records,
                "timestamp": now,
                "expires_at": expires_at,
            }

            if records:
                try:
                    df = pd.DataFrame(records)
                    if not df.empty:
                        db_manager.save_historical_data(
                            request.symbol, df, bar_size=request.bar_size
                        )
                        filename = f"data/{request.symbol}_historical.csv"
                        try:
                            os.makedirs(os.path.dirname(filename), exist_ok=True)
                            df.to_csv(filename, index=False)
                            service_result.setdefault("filename", filename)
                        except Exception as exc:
                            logger.warning(
                                "Failed to write CSV backup for historical data: {}",
                                exc,
                            )
                except Exception as exc:
                    logger.debug(
                        "Skipping historical data persistence for %s due to %s",
                        request.symbol,
                        exc,
                    )

        service_result.setdefault("success", True)
        service_result.setdefault("rows", len(service_result.get("data", [])) if isinstance(service_result.get("data"), list) else 0)
        service_result.setdefault("source", "market_data_service")
        return service_result

    except HTTPException as exc:
        # Preserve explicit HTTP errors (e.g. 404/408) so callers can handle
        # them without the response being wrapped into a 500.
        raise exc
    except Exception as e:
        logger.error(f"Historical data error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/chart_data")
async def get_chart_data(
    request: ChartDataRequest,
    user: Optional[str] = Depends(get_current_user),
):
    """获取图表数据"""
    try:
        mode = get_data_layer_mode()
        manager = None if is_legacy_mode(mode) else _get_data_layer_manager()

        # 统一使用与历史接口一致的缓存策略，使用前缀避免冲突
        _clean_expired_cache()
        now = now_local()
        use_cache = True if (getattr(request, "use_cache", True) is not False) else False
        force_refresh = True if (getattr(request, "force_refresh", False) is True) else False

        cache_key_base = f"{request.symbol}_{request.duration}_{request.bar_size}_{request.exchange}"
        cache_key = f"chart::{cache_key_base}"

        # 1) 内存缓存命中（仅当使用缓存且不强制刷新）
        if use_cache and not force_refresh and cache_key in historical_data_cache:
            cache_entry = historical_data_cache[cache_key]
            if cache_entry.get("expires_at", now) > now:
                logger.debug(f"Using chart memory cache for {request.symbol}")
                cached_data = cache_entry.get("data", [])
                return {
                    "success": True,
                    "data": cached_data,
                    "symbol": request.symbol,
                    "duration": request.duration,
                    "bar_size": request.bar_size,
                }

        # 2) 数据库缓存（覆盖请求范围且不强制刷新时）
        db_manager = get_db_manager()
        interval = db_manager._normalize_interval(request.bar_size)
        minutes = _parse_duration_to_minutes(request.duration)
        start_dt = now - timedelta(minutes=minutes)

        cache_df = db_manager.query_historical_cache(
            request.symbol,
            interval=interval,
            start_dt=start_dt.isoformat(timespec="seconds"),
            end_dt=now.isoformat(timespec="seconds"),
        )

        if use_cache and not force_refresh and not db_manager.detect_missing_ranges(
            cache_df, start_dt.isoformat(), now.isoformat(), interval
        ):
            logger.debug(f"Using chart database cache for {request.symbol}")
            chart_data = []
            try:
                records = cache_df.to_dict("records")
                for row in records:
                    raw_time = row.get("date") or row.get("time")
                    t = to_local_time(raw_time)
                    chart_data.append(
                        {
                            "time": t,
                            "open": float(row.get("open", 0) or 0),
                            "high": float(row.get("high", 0) or 0),
                            "low": float(row.get("low", 0) or 0),
                            "close": float(row.get("close", 0) or 0),
                            "volume": int(row.get("volume", 0) or 0),
                        }
                    )
            except Exception:
                chart_data = []

            if chart_data:
                expires_at = now + timedelta(minutes=CACHE_DURATION_MINUTES)
                historical_data_cache[cache_key] = {
                    "data": chart_data,
                    "timestamp": now,
                    "expires_at": expires_at,
                }
                return {
                    "success": True,
                    "data": chart_data,
                    "symbol": request.symbol,
                    "duration": request.duration,
                    "bar_size": request.bar_size,
                }

        # 3) 需要拉取新数据（强制刷新或缓存不足）
        interval_key = _map_bar_size_to_interval(request.bar_size)
        duration_minutes = _parse_duration_to_minutes(request.duration)

        unified_chart: Optional[List[Dict[str, Any]]] = None
        if manager and interval_key:
            try:
                candles = await manager.history(
                    DataSubscriptionRequest(
                        channel="market.tick",
                        symbol=(request.symbol or "").upper(),
                        interval=interval_key,
                        options={"exchange": request.exchange},
                    )
                )
            except DataSourceError as exc:
                logger.debug(
                    "Unified chart history unavailable for {}: {}",
                    request.symbol,
                    exc,
                )
                candles = []
            except Exception as exc:
                logger.exception(
                    "Unified chart history error for {}: {}",
                    request.symbol,
                    exc,
                )
                candles = []

            if candles:
                candles = _filter_candles_by_duration(candles, duration_minutes)
                records = _candles_to_records(candles)
                if records:
                    chart_data = []
                    for row in records:
                        chart_data.append(
                            {
                                "time": row["date"],
                                "open": float(row.get("open", 0) or 0),
                                "high": float(row.get("high", 0) or 0),
                                "low": float(row.get("low", 0) or 0),
                                "close": float(row.get("close", 0) or 0),
                                "volume": int(row.get("volume", 0) or 0),
                            }
                        )
                    unified_chart = chart_data

        if unified_chart:
            expires_at = now + timedelta(minutes=CACHE_DURATION_MINUTES)
            historical_data_cache[cache_key] = {
                "data": unified_chart,
                "timestamp": now,
                "expires_at": expires_at,
            }
            return {
                "success": True,
                "data": unified_chart,
                "symbol": request.symbol,
                "duration": request.duration,
                "bar_size": request.bar_size,
            }

        client = _get_market_data_service_client()
        service_payload = request.model_dump(exclude_none=True)
        alias_payload = request.model_dump(by_alias=True, exclude_none=True)
        service_payload.update(alias_payload)

        try:
            service_result = await client.request(
                "POST", "/chart_data", json=service_payload
            )
        except ServiceUnavailableError:
            raise HTTPException(
                status_code=503, detail="Market data service unavailable"
            )
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
                "Market data chart request failed for {}: {}",
                request.symbol,
                exc,
            )
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        if not isinstance(service_result, Mapping):
            raise HTTPException(
                status_code=502,
                detail="Unexpected chart data format from market data service",
            )

        chart_data = service_result.get("data")
        if isinstance(chart_data, list):
            if chart_data and use_cache:
                expires_at = now + timedelta(minutes=CACHE_DURATION_MINUTES)
                historical_data_cache[cache_key] = {
                    "data": chart_data,
                    "timestamp": now,
                    "expires_at": expires_at,
                }
            service_result.setdefault("rows", len(chart_data))
        service_result.setdefault("success", True)
        service_result.setdefault("symbol", request.symbol)
        service_result.setdefault("duration", request.duration)
        service_result.setdefault("bar_size", request.bar_size)

        if chart_data is not None:
            return service_result

        # 5) API返回空，回退数据库load_historical_data
        fallback_df = None
        try:
            fallback_df = db_manager.load_historical_data(
                request.symbol, bar_size=request.bar_size
            )
        except Exception:
            fallback_df = None
        if fallback_df is not None and not fallback_df.empty:
            chart_data = []
            try:
                for ts, row in fallback_df.iterrows():
                    t = to_local_time(ts)
                    chart_data.append(
                        {
                            "time": t,
                            "open": float(row.get("open", 0) or 0),
                            "high": float(row.get("high", 0) or 0),
                            "low": float(row.get("low", 0) or 0),
                            "close": float(row.get("close", 0) or 0),
                            "volume": int(row.get("volume", 0) or 0),
                        }
                    )
            except Exception:
                chart_data = []
            if chart_data:
                return {
                    "success": True,
                    "data": chart_data,
                    "symbol": request.symbol,
                    "duration": request.duration,
                    "bar_size": request.bar_size,
                }

        raise HTTPException(status_code=404, detail="No chart data found")

    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        logger.error(f"Chart data error: {e}")
        detail = str(e) if str(e) else "Internal error"
        raise HTTPException(status_code=500, detail=detail)


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
):
    """获取当前价格 - 轻量级接口"""
    mode = get_data_layer_mode()
    manager = None if is_legacy_mode(mode) else _get_data_layer_manager()

    if CURRENT_PRICE_CACHE_ONLY:
        return {
            "success": False,
            "symbol": request.symbol,
            "price": None,
            "timestamp": now_local().isoformat(),
            "error": "Cache-only mode not supported without market data service",
        }

    if manager:
        try:
            snapshot = await manager.snapshot(
                DataSubscriptionRequest(
                    channel="market.tick",
                    symbol=(request.symbol or "").upper(),
                    options={"exchange": request.exchange},
                )
            )
        except DataSourceError as exc:
            logger.debug(
                "Unified snapshot unavailable for {}: {}",
                request.symbol,
                exc,
            )
            snapshot = {}
        except Exception as exc:
            logger.exception(
                "Unified snapshot error for {}: {}",
                request.symbol,
                exc,
            )
            snapshot = {}
        if snapshot and snapshot.get("last") is not None:
            return {
                "success": True,
                "symbol": request.symbol,
                "price": snapshot["last"],
                "timestamp": now_local().isoformat(),
                "source": "data_layer",
            }

    try:
        payload = await client.fetch_current_price(request.symbol)
    except ServiceUnavailableError:
        raise HTTPException(status_code=503, detail="Market data service unavailable")
    except ServiceResponseError as exc:
        if 500 <= exc.status_code < 600:
            raise HTTPException(status_code=502, detail="Market data service error")
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail or "Market data service error",
        )
    except ServiceClientError as exc:
        logger.error("Market data current price request failed for {}: {}", request.symbol, exc)
        raise HTTPException(status_code=502, detail=str(exc))

    response: Dict[str, Any]
    if isinstance(payload, Mapping):
        response = dict(payload)
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
