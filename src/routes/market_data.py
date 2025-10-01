import asyncio
import contextlib
import inspect
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, Any, Optional, List

import httpx
import websockets

import pandas as pd
from fastapi import APIRouter, HTTPException, Depends, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from loguru import logger

import core
from core import connection_manager, get_db_manager
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

router = APIRouter()

# Cache controls
CURRENT_PRICE_CACHE_ONLY = os.getenv("CURRENT_PRICE_CACHE_ONLY", "False").lower() == "true"
MARKET_DATA_SERVICE_URL = os.getenv("MARKET_DATA_SERVICE_URL", "http://localhost:8010").rstrip("/")
MARKET_DATA_SERVICE_WS_URL = os.getenv("MARKET_DATA_SERVICE_WS_URL")
MARKET_DATA_SERVICE_TIMEOUT = float(os.getenv("MARKET_DATA_SERVICE_TIMEOUT", "5.0"))
historical_data_cache: Dict[str, Dict[str, Any]] = {}
CACHE_DURATION_MINUTES = 1  # 缓存1分钟
INDEX_FUTURE_ROOTS = {"ES", "MES", "NQ", "MNQ", "YM", "MYM", "RTY", "M2K", "VX"}
NY_TZ = ZoneInfo("America/New_York")

_data_layer_manager = None

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


def _market_service_base_url() -> str:
    return MARKET_DATA_SERVICE_URL or "http://localhost:8010"


def _market_service_ws_url() -> str:
    if MARKET_DATA_SERVICE_WS_URL:
        return MARKET_DATA_SERVICE_WS_URL.rstrip("/")
    base = _market_service_base_url()
    if "://" not in base:
        base = f"http://{base}"
    scheme, rest = base.split("://", 1)
    ws_scheme = "wss" if scheme == "https" else "ws"
    return f"{ws_scheme}://{rest.rstrip('/')}/ws/market"


async def _market_service_get(path: str) -> httpx.Response:
    async with httpx.AsyncClient(
        base_url=_market_service_base_url(),
        timeout=MARKET_DATA_SERVICE_TIMEOUT,
    ) as client:
        return await client.get(path)


async def _market_service_post(path: str, payload: Dict[str, Any]) -> httpx.Response:
    async with httpx.AsyncClient(
        base_url=_market_service_base_url(),
        timeout=MARKET_DATA_SERVICE_TIMEOUT,
    ) as client:
        return await client.post(path, json=payload)


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


def _require_ib_client():
    """Ensure an IBKR client instance is available before servicing the request."""
    ib_client = getattr(core, "ibkr_client", None)
    if ib_client is None:
        raise HTTPException(status_code=400, detail="Not connected to IBKR")
    return ib_client


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


async def _fetch_ib_historical(
    ib_client,
    *,
    symbol: str,
    duration: str,
    bar_size: str,
    exchange: str,
    timeout: float,
):
    """Call IB client's historical data API with coroutine/sync compatibility."""

    method = getattr(ib_client, "get_historical_data", None)
    if method is None:
        return None

    if inspect.iscoroutinefunction(method):
        return await asyncio.wait_for(
            method(
                symbol=symbol,
                duration=duration,
                bar_size=bar_size,
                exchange=exchange,
            ),
            timeout=timeout,
        )

    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(
            None,
            lambda: method(
                symbol=symbol,
                duration=duration,
                bar_size=bar_size,
                exchange=exchange,
            ),
        ),
        timeout=timeout,
    )


@router.post("/api/historical_data")
async def get_historical_data(
    request: HistoricalDataRequest,
    user: Optional[str] = Depends(get_current_user),
):
    """获取历史数据"""
    mode = get_data_layer_mode()
    manager = None if is_legacy_mode(mode) else _get_data_layer_manager()

    if manager is None and not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")

    ib_client = None
    if manager is None or connection_manager.is_connected():
        try:
            ib_client = _require_ib_client()
        except HTTPException:
            if manager is None:
                raise

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

        if ib_client is None:
            raise HTTPException(status_code=404, detail="No data found")

        try:
            df = await _fetch_ib_historical(
                ib_client,
                symbol=request.symbol,
                duration=request.duration,
                bar_size=request.bar_size,
                exchange=request.exchange,
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            logger.error(
                f"Historical data request timed out for {request.symbol}"
            )
            raise HTTPException(
                status_code=408, detail="Historical data request timed out"
            )

        if df is None or getattr(df, "empty", False):
            if cache_df is not None and not cache_df.empty:
                return {
                    "success": True,
                    "data": cache_df.to_dict("records"),
                    "filename": None,
                    "rows": len(cache_df),
                }
            raise HTTPException(status_code=404, detail="No data found")

        db_manager.save_historical_data(request.symbol, df, bar_size=request.bar_size)
        data_dict = df.to_dict("records")
        expires_at = now + timedelta(minutes=CACHE_DURATION_MINUTES)
        historical_data_cache[cache_key] = {
            "data": data_dict,
            "timestamp": now,
            "expires_at": expires_at,
        }
        logger.debug(
            f"Cached historical data for {request.symbol}, expires at {expires_at}"
        )

        filename = f"data/{request.symbol}_historical.csv"
        try:
            os.makedirs(os.path.dirname(filename), exist_ok=True)
            df.to_csv(filename, index=False)
        except Exception as e:
            logger.warning(
                f"Failed to write CSV backup for historical data: {e}"
            )

        return {
            "success": True,
            "data": data_dict,
            "filename": filename,
            "rows": len(df),
            "source": "ibkr_api",
        }

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

        service_error: Optional[Exception] = None
        try:
            service_payload = request.model_dump(exclude_none=True)
            alias_payload = request.model_dump(by_alias=True, exclude_none=True)
            service_payload.update(alias_payload)
            response = await _market_service_post("/chart_data", service_payload)
        except httpx.RequestError as exc:
            service_error = exc
            logger.error(
                "Market data service request failed for {}: {}",
                request.symbol,
                exc,
            )
        else:
            if response.status_code == 404:
                logger.debug(
                    "Market data service returned 404 for {}",
                    request.symbol,
                )
            elif response.status_code >= 500:
                logger.error(
                    "Market data service error",
                    status=response.status_code,
                    body=response.text,
                )
                raise HTTPException(
                    status_code=502, detail="Market data service error"
                )
            elif not response.is_success:
                detail = response.text or "Failed to load chart data"
                raise HTTPException(status_code=response.status_code, detail=detail)
            else:
                try:
                    service_result = response.json()
                except ValueError as exc:
                    logger.error(
                        "Invalid JSON from market data service",
                        error=str(exc),
                    )
                    raise HTTPException(
                        status_code=502,
                        detail="Invalid response from market data service",
                    ) from exc

                if not isinstance(service_result, dict):
                    raise HTTPException(
                        status_code=502,
                        detail="Unexpected chart data format from market data service",
                    )

                chart_data = service_result.get("data")
                if isinstance(chart_data, list) and chart_data and use_cache:
                    expires_at = now + timedelta(minutes=CACHE_DURATION_MINUTES)
                    historical_data_cache[cache_key] = {
                        "data": chart_data,
                        "timestamp": now,
                        "expires_at": expires_at,
                    }

                service_result.setdefault("success", True)
                service_result.setdefault("symbol", request.symbol)
                service_result.setdefault("duration", request.duration)
                service_result.setdefault("bar_size", request.bar_size)
                return service_result

        if manager is None and not connection_manager.is_connected():
            if service_error is not None:
                raise HTTPException(
                    status_code=503, detail="Market data service unavailable"
                )
            reason = None
            try:
                reason = connection_manager.get_last_connect_error()
            except Exception:
                reason = None
            detail = "Not connected to IBKR"
            if reason:
                detail = f"{detail}: {reason}"
            raise HTTPException(status_code=400, detail=detail)

        if not connection_manager.is_connected():
            if service_error is not None:
                raise HTTPException(
                    status_code=503, detail="Market data service unavailable"
                )
            raise HTTPException(status_code=400, detail="Not connected to IBKR")

        ib_client = getattr(core, "ibkr_client", None)
        if ib_client is None:
            raise HTTPException(status_code=400, detail="IBKR client unavailable")

        try:
            df = await _fetch_ib_historical(
                ib_client,
                symbol=request.symbol,
                duration=request.duration,
                bar_size=request.bar_size,
                exchange=request.exchange,
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"Chart data historical fetch timed out for {request.symbol}"
            )
            df = None

        # 4) 成功获取数据
        if df is not None and not df.empty:
            try:
                db_manager.save_historical_data(
                    request.symbol, df, bar_size=request.bar_size
                )
            except Exception as e:
                logger.warning(f"Failed to save chart data: {e}")

            chart_data = []
            for _, row in df.iterrows():
                raw_time = row["date"]
                if "time" in df.columns and not pd.isna(row.get("time")):
                    raw_time = f"{row['date']} {row['time']}"
                t = to_local_time(raw_time)
                chart_data.append(
                    {
                        "time": t,
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "volume": int(row["volume"]) if "volume" in row else 0,
                    }
                )

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

        reason = None
        try:
            reason = connection_manager.get_last_connect_error()
        except Exception:
            reason = None
        raise HTTPException(
            status_code=404,
            detail=(
                "No chart data found"
                + (f". Possible cause: {reason}" if reason else "")
            ),
        )

    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        logger.error(f"Chart data error: {e}")
        reason = None
        try:
            reason = connection_manager.get_last_connect_error()
        except Exception:
            reason = None
        detail = (str(e) if str(e) else "Internal error") + (
            f" | {reason}" if reason else ""
        )
        raise HTTPException(status_code=500, detail=detail)


@router.get("/api/realtime_data/{symbol}")
async def get_realtime_data(
    symbol: str,
    exchange: str = "SMART",
    user: Optional[str] = Depends(get_current_user),
):
    """获取实时数据"""
    try:
        response = await _market_service_get(f"/market/{symbol}")
    except httpx.RequestError as exc:
        logger.error("Market data service request failed", symbol=symbol, error=str(exc))
        raise HTTPException(status_code=503, detail="Market data service unavailable") from exc

    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="No data available")
    if response.status_code >= 500:
        logger.error("Market data service error", status=response.status_code, body=response.text)
        raise HTTPException(status_code=502, detail="Market data service error")
    if not response.is_success:
        detail = response.text or "Failed to load market data"
        raise HTTPException(status_code=response.status_code, detail=detail)

    try:
        payload = response.json()
    except ValueError as exc:
        logger.error("Invalid JSON from market data service", error=str(exc))
        raise HTTPException(status_code=502, detail="Invalid response from market data service") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="Unexpected market data format")

    return {"success": True, "data": payload}

@router.post("/api/current_price")
async def get_current_price(
    request: CurrentPriceRequest,
    user: Optional[str] = Depends(get_current_user),
):
    """获取当前价格 - 轻量级接口"""
    ib_client = getattr(core, "ibkr_client", None)
    mode = get_data_layer_mode()
    manager = None if is_legacy_mode(mode) else _get_data_layer_manager()

    if CURRENT_PRICE_CACHE_ONLY:
        if ib_client and hasattr(ib_client, "price_cache") and request.symbol in ib_client.price_cache:
            cached_price = ib_client.price_cache.get(request.symbol)
            if cached_price is not None:
                return {
                    "success": True,
                    "symbol": request.symbol,
                    "price": cached_price,
                    "timestamp": now_local().isoformat(),
                    "source": "cache_only",
                }
        return {
            "success": False,
            "symbol": request.symbol,
            "price": None,
            "timestamp": now_local().isoformat(),
            "error": "Cache-only mode: no cached price",
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
        if snapshot and snapshot.get("last"):
            return {
                "success": True,
                "symbol": request.symbol,
                "price": snapshot["last"],
                "timestamp": now_local().isoformat(),
                "source": "data_layer",
            }

    if not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")

    ib_client = _require_ib_client()

    try:
        if hasattr(ib_client, "price_cache") and request.symbol in ib_client.price_cache:
            cached_price = ib_client.price_cache.get(request.symbol)
            if cached_price:
                return {
                    "success": True,
                    "symbol": request.symbol,
                    "price": cached_price,
                    "timestamp": now_local().isoformat(),
                    "source": "cache",
                }

        try:
            data = await ib_client.get_real_time_data(
                        request.symbol, request.exchange
                    )

            if data and "last" in data:
                price = data["last"]
                return {
                    "success": True,
                    "symbol": request.symbol,
                    "price": price,
                    "timestamp": now_local().isoformat(),
                    "source": "realtime",
                }
        except asyncio.TimeoutError:
            logger.warning(
                f"Current price request for {request.symbol} timed out"
            )

        symbol_upper = (request.symbol or '').upper()
        timeout_duration = 3.0 if any(symbol_upper.startswith(root) for root in INDEX_FUTURE_ROOTS) else 2.0
        try:
            df = await _fetch_ib_historical(
                ib_client,
                symbol=request.symbol,
                duration="1 D",
                bar_size="1 min",
                exchange=request.exchange,
                timeout=timeout_duration,
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"Historical data fallback for {request.symbol} timed out"
            )
            df = None

        if df is not None and not df.empty:
            latest_price = float(df.iloc[-1]["close"])
            return {
                "success": True,
                "symbol": request.symbol,
                "price": latest_price,
                "timestamp": now_local().isoformat(),
                "source": "historical",
            }

        return {
            "success": False,
            "symbol": request.symbol,
            "price": None,
            "timestamp": now_local().isoformat(),
            "error": "Unable to get current price",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Current price error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.websocket("/ws/market")
async def proxy_market_data_stream(websocket: WebSocket) -> None:
    """Proxy websocket updates from the market data service to UI clients."""

    upstream_url = _market_service_ws_url()
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
            await websocket.close(code=1011, reason="Upstream connection failed")
    finally:
        with contextlib.suppress(Exception):
            await websocket.close()
