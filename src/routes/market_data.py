import asyncio
import os
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

import pandas as pd
from fastapi import APIRouter, HTTPException, Depends
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

router = APIRouter()

# Cache controls
CURRENT_PRICE_CACHE_ONLY = os.getenv("CURRENT_PRICE_CACHE_ONLY", "False").lower() == "true"
historical_data_cache: Dict[str, Dict[str, Any]] = {}
CACHE_DURATION_MINUTES = 1  # 缓存1分钟


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
    if not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")

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

        loop = asyncio.get_event_loop()
        try:
            df = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: core.ibkr_client.get_historical_data(
                        symbol=request.symbol,
                        duration=request.duration,
                        bar_size=request.bar_size,
                        exchange=request.exchange,
                    ),
                ),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            logger.error(
                f"Historical data request timed out for {request.symbol}"
            )
            raise HTTPException(
                status_code=408, detail="Historical data request timed out"
            )

        if df.empty:
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
        if not connection_manager.is_connected():
            reason = None
            try:
                reason = connection_manager.get_last_connect_error()
            except Exception:
                reason = None
            detail = "Not connected to IBKR"
            if reason:
                detail = f"{detail}: {reason}"
            raise HTTPException(status_code=400, detail=detail)

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
        df = await core.ibkr_client.get_historical_data(
                    symbol=request.symbol,
                    duration=request.duration,
                    bar_size=request.bar_size,
                    exchange=request.exchange,
                )

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
    if not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")

    try:
        loop = asyncio.get_event_loop()
        try:
            data = await core.ibkr_client.get_real_time_data(symbol, exchange)
        except asyncio.TimeoutError:
            logger.warning(
                f"Real-time data request for {symbol} timed out, checking cache"
            )
            cached_data = None
            if hasattr(core.ibkr_client, "price_cache") and symbol in core.ibkr_client.price_cache:
                cached_price = core.ibkr_client.price_cache.get(symbol)
                if cached_price:
                    cached_data = {
                        "symbol": symbol,
                        "last": cached_price,
                        "bid": 0,
                        "ask": 0,
                        "volume": 0,
                        "timestamp": now_local().isoformat(),
                        "is_cached": True,
                    }
            if cached_data:
                logger.info(f"Returning cached data for {symbol}: {cached_price}")
                return {"success": True, "data": cached_data}
            else:
                logger.warning(
                    f"No cached data available for {symbol}"
                )
                return {
                    "success": False,
                    "error": "Real-time data request timed out and no cached data available",
                }

        if data:
            return {"success": True, "data": data}
        else:
            if hasattr(core.ibkr_client, "price_cache") and symbol in core.ibkr_client.price_cache:
                cached_price = core.ibkr_client.price_cache.get(symbol)
                if cached_price:
                    cached_data = {
                        "symbol": symbol,
                        "last": cached_price,
                        "bid": 0,
                        "ask": 0,
                        "volume": 0,
                        "timestamp": now_local().isoformat(),
                        "is_cached": True,
                    }
                    logger.info(
                        f"No fresh data for {symbol}, returning cached: {cached_price}"
                    )
                    return {"success": True, "data": cached_data}
            raise HTTPException(status_code=404, detail="No data available")

    except HTTPException as exc:
        raise exc
    except Exception as e:
        logger.error(f"Real-time data error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/current_price")
async def get_current_price(
    request: CurrentPriceRequest,
    user: Optional[str] = Depends(get_current_user),
):
    """获取当前价格 - 轻量级接口"""
    if CURRENT_PRICE_CACHE_ONLY:
        if hasattr(core.ibkr_client, "price_cache") and request.symbol in core.ibkr_client.price_cache:
            cached_price = core.ibkr_client.price_cache.get(request.symbol)
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

    if not connection_manager.is_connected():
        raise HTTPException(status_code=400, detail="Not connected to IBKR")

    try:
        if hasattr(core.ibkr_client, "price_cache") and request.symbol in core.ibkr_client.price_cache:
            cached_price = core.ibkr_client.price_cache.get(request.symbol)
            if cached_price:
                return {
                    "success": True,
                    "symbol": request.symbol,
                    "price": cached_price,
                    "timestamp": now_local().isoformat(),
                    "source": "cache",
                }

        loop = asyncio.get_event_loop()
        try:
            data = await core.ibkr_client.get_real_time_data(
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

        try:
            timeout_duration = 3.0 if request.symbol == "MNQ" else 2.0
            df = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: core.ibkr_client.get_historical_data(
                        symbol=request.symbol,
                        duration="1 D",
                        bar_size="1 min",
                        exchange=request.exchange,
                    ),
                ),
                timeout=timeout_duration,
            )

            if df is not None and not df.empty:
                latest_price = float(df.iloc[-1]["close"])
                return {
                    "success": True,
                    "symbol": request.symbol,
                    "price": latest_price,
                    "timestamp": now_local().isoformat(),
                    "source": "historical",
                }
        except asyncio.TimeoutError:
            logger.warning(
                f"Historical data fallback for {request.symbol} timed out"
            )

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
