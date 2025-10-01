import csv
import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, Any, Optional, List, Iterable

import httpx
from fastapi import APIRouter, HTTPException, Depends
from loguru import logger

from core import get_core_container
from ibbot.redis import ensure_redis
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

core_container = get_core_container()
router = APIRouter()

# Cache controls
CURRENT_PRICE_CACHE_ONLY = os.getenv("CURRENT_PRICE_CACHE_ONLY", "False").lower() == "true"
historical_data_cache: Dict[str, Dict[str, Any]] = {}
CACHE_DURATION_MINUTES = 1  # 缓存1分钟
INDEX_FUTURE_ROOTS = {"ES", "MES", "NQ", "MNQ", "YM", "MYM", "RTY", "M2K", "VX"}
NY_TZ = ZoneInfo("America/New_York")

_data_layer_manager = None

MARKET_DATA_SERVICE_URL = (
    os.getenv("MARKET_DATA_SERVICE_URL")
    or os.getenv("MARKET_DATA_API_URL")
    or os.getenv("MARKET_DATA_BASE_URL")
)
MARKET_DATA_HISTORY_PATH = os.getenv(
    "MARKET_DATA_HISTORY_PATH", "/market/history"
)
MARKET_DATA_SNAPSHOT_PATH = os.getenv(
    "MARKET_DATA_SNAPSHOT_PATH", "/market/{symbol}"
)
MARKET_DATA_TIMEOUT = float(os.getenv("MARKET_DATA_SERVICE_TIMEOUT", "5.0"))
MARKET_DATA_STREAM_KEY = os.getenv(
    "MARKET_DATA_STREAM_KEY", "stream:market-service"
)
MARKET_DATA_SNAPSHOT_PREFIX = os.getenv(
    "MARKET_DATA_SNAPSHOT_PREFIX", "hash:market:snapshot"
)
MAX_HISTORY_RECORDS = int(os.getenv("MARKET_DATA_MAX_HISTORY", "2000"))

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


def _coerce_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=NY_TZ)
        return value.astimezone(NY_TZ)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=NY_TZ)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            try:
                return datetime.fromtimestamp(int(text), tz=NY_TZ)
            except (OverflowError, OSError, ValueError):
                return None
        try:
            return datetime.fromisoformat(text).astimezone(NY_TZ)
        except ValueError:
            pass
        try:
            return datetime.fromtimestamp(float(text), tz=NY_TZ)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _normalise_candle_record(candle: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(candle, dict):
        return None

    ts_candidate = (
        candle.get("interval_end")
        or candle.get("interval_end_ns")
        or candle.get("interval_start")
        or candle.get("interval_start_ns")
        or candle.get("timestamp")
        or candle.get("ts")
        or candle.get("time")
    )
    timestamp = None
    if ts_candidate is not None and str(ts_candidate).isdigit() and len(str(ts_candidate)) > 12:
        try:
            timestamp = datetime.fromtimestamp(
                int(ts_candidate) / 1_000_000_000, tz=NY_TZ
            )
        except (OverflowError, OSError, ValueError):
            timestamp = None
    if timestamp is None:
        timestamp = _parse_timestamp(ts_candidate)
    if timestamp is None:
        return None

    record = {
        "date": to_local_iso(timestamp),
        "open": _coerce_float(
            candle.get("open")
            or candle.get("o")
            or candle.get("price_open")
        ),
        "high": _coerce_float(
            candle.get("high")
            or candle.get("h")
            or candle.get("price_high")
        ),
        "low": _coerce_float(
            candle.get("low")
            or candle.get("l")
            or candle.get("price_low")
        ),
        "close": _coerce_float(
            candle.get("close")
            or candle.get("c")
            or candle.get("price_close")
        ),
        "volume": _coerce_int(
            candle.get("volume")
            or candle.get("v")
            or candle.get("trade_volume")
        ),
    }

    return record


def _candles_to_records(candles: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for candle in candles:
        normalised = _normalise_candle_record(candle)
        if normalised is not None:
            records.append(normalised)
    records.sort(key=lambda item: item.get("date") or "")
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


async def _fetch_service_json(path: str, *, params: Optional[Dict[str, Any]] = None) -> Any:
    base_url = MARKET_DATA_SERVICE_URL
    if not base_url:
        return None

    url = f"{base_url.rstrip('/')}{path}"

    try:
        async with httpx.AsyncClient(timeout=MARKET_DATA_TIMEOUT) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Market data service HTTP error",
            url=url,
            status=exc.response.status_code,
        )
        return None
    except httpx.RequestError as exc:
        logger.warning(
            "Market data service request failed",
            url=url,
            error=str(exc),
        )
        return None

    try:
        return response.json()
    except ValueError:
        logger.warning("Market data service returned invalid JSON", url=url)
        return None


def _normalise_snapshot_payload(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(data, dict):
        return None

    symbol = data.get("symbol") or data.get("ticker")
    price = _coerce_float(data.get("price") or data.get("last") or data.get("close"))
    bid = _coerce_float(data.get("bid"))
    ask = _coerce_float(data.get("ask"))
    volume = _coerce_int(data.get("volume")) or 0

    timestamp_value = (
        data.get("timestamp")
        or data.get("updated_at")
        or data.get("ts")
        or data.get("time")
    )
    timestamp = _parse_timestamp(timestamp_value) or now_local()

    result = {
        "symbol": symbol,
        "last": price,
        "bid": bid,
        "ask": ask,
        "volume": volume,
        "timestamp": to_local_iso(timestamp),
    }

    if data.get("stale") is not None:
        result["stale"] = bool(data.get("stale"))
    if data.get("source"):
        result["source"] = data.get("source")

    return result


async def _fetch_snapshot_from_service(symbol: str) -> Optional[Dict[str, Any]]:
    path = MARKET_DATA_SNAPSHOT_PATH.format(symbol=symbol)
    payload = await _fetch_service_json(path)
    if isinstance(payload, dict):
        if "data" in payload and isinstance(payload["data"], dict):
            payload = payload["data"]
        return _normalise_snapshot_payload(payload)
    return None


async def _fetch_snapshot_from_redis(symbol: str) -> Optional[Dict[str, Any]]:
    redis = await ensure_redis()
    if redis is None:
        return None

    key = f"{MARKET_DATA_SNAPSHOT_PREFIX}:{symbol.upper()}"
    try:
        raw = await redis.hgetall(key)
    except Exception as exc:
        logger.warning("Failed to read market snapshot", symbol=symbol, error=str(exc))
        return None

    if not raw:
        return None

    decoded: Dict[str, Any] = {}
    for key_field, value in raw.items():
        if isinstance(key_field, bytes):
            key_field = key_field.decode("utf-8", errors="ignore")
        if isinstance(value, bytes):
            try:
                value = value.decode("utf-8")
            except Exception:
                value = value.decode("latin-1", errors="ignore")
        decoded[key_field] = value

    dom_value = decoded.get("dom")
    if isinstance(dom_value, str):
        try:
            decoded["dom"] = json.loads(dom_value)
        except json.JSONDecodeError:
            pass

    if "data" in decoded and isinstance(decoded["data"], str):
        try:
            decoded["data"] = json.loads(decoded["data"])
        except json.JSONDecodeError:
            pass

    result = _normalise_snapshot_payload(decoded)
    if result is not None and decoded.get("stale") is not None:
        result["stale"] = bool(decoded.get("stale"))
    return result


async def _fetch_market_snapshot(symbol: str) -> Optional[Dict[str, Any]]:
    snapshot = await _fetch_snapshot_from_service(symbol)
    if snapshot:
        snapshot.setdefault("source", "service")
        return snapshot

    snapshot = await _fetch_snapshot_from_redis(symbol)
    if snapshot:
        snapshot.setdefault("source", "redis")
        return snapshot

    return None


def _limit_for_duration(duration_minutes: int) -> int:
    if duration_minutes <= 0:
        return min(500, MAX_HISTORY_RECORDS)
    approx = max(1, duration_minutes)
    return min(MAX_HISTORY_RECORDS, approx * 5)


def _extract_history_payload(payload: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("candles", "bars", "data", "history"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return []
    if isinstance(payload, list):
        return payload
    return []


async def _fetch_history_from_service(
    symbol: str,
    *,
    interval: Optional[str],
    duration_minutes: int,
    exchange: str,
) -> List[Dict[str, Any]]:
    path = MARKET_DATA_HISTORY_PATH
    params: Dict[str, Any] = {"symbol": symbol}
    if interval:
        params["interval"] = interval
    if duration_minutes > 0:
        params["duration_minutes"] = duration_minutes
    if exchange:
        params["exchange"] = exchange

    payload = await _fetch_service_json(path, params=params)
    records = _candles_to_records(_extract_history_payload(payload))
    return records


async def _fetch_history_from_stream(
    symbol: str,
    *,
    interval: Optional[str],
    duration_minutes: int,
) -> List[Dict[str, Any]]:
    redis = await ensure_redis()
    if redis is None:
        return []

    count = _limit_for_duration(duration_minutes)
    try:
        entries = await redis.xrevrange(MARKET_DATA_STREAM_KEY, count=count)
    except Exception as exc:
        logger.warning("Failed to read market data stream", error=str(exc))
        return []

    records: List[Dict[str, Any]] = []
    symbol_upper = symbol.upper()
    seen_dates: set[str] = set()

    for _, data in entries:
        if not isinstance(data, dict):
            continue
        decoded: Dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(key, bytes):
                key = key.decode("utf-8", errors="ignore")
            if isinstance(value, bytes):
                try:
                    value = value.decode("utf-8")
                except Exception:
                    value = value.decode("latin-1", errors="ignore")
            decoded[key] = value

        entry_symbol = str(decoded.get("symbol") or "").upper()
        if entry_symbol != symbol_upper:
            continue

        event_type = str(decoded.get("type") or decoded.get("event_type") or "")
        if not event_type.startswith("market.candle"):
            continue

        payload_raw = decoded.get("data")
        if isinstance(payload_raw, str):
            try:
                payload_data = json.loads(payload_raw)
            except json.JSONDecodeError:
                continue
        elif isinstance(payload_raw, dict):
            payload_data = payload_raw
        else:
            continue

        if interval:
            payload_interval = (
                payload_data.get("interval")
                or decoded.get("interval")
                or event_type.rsplit(".", 1)[-1]
            )
            if payload_interval and str(payload_interval).lower() != interval.lower():
                continue

        normalised = _normalise_candle_record(payload_data)
        if normalised is None:
            continue

        date_key = normalised.get("date")
        if date_key in seen_dates:
            continue
        seen_dates.add(date_key)
        records.append(normalised)

    records.sort(key=lambda item: item.get("date") or "")
    return records


async def _fetch_market_history(
    symbol: str,
    *,
    interval: Optional[str],
    duration_minutes: int,
    exchange: str,
) -> List[Dict[str, Any]]:
    records = await _fetch_history_from_service(
        symbol,
        interval=interval,
        duration_minutes=duration_minutes,
        exchange=exchange,
    )
    if records:
        return records

    records = await _fetch_history_from_stream(
        symbol,
        interval=interval,
        duration_minutes=duration_minutes,
    )
    return records


def _write_records_to_csv(filename: str, records: Iterable[Dict[str, Any]]) -> None:
    if not records:
        return

    try:
        os.makedirs(os.path.dirname(filename), exist_ok=True)
        with open(filename, "w", newline="") as fp:
            fieldnames = ["date", "open", "high", "low", "close", "volume"]
            writer = csv.DictWriter(fp, fieldnames=fieldnames)
            writer.writeheader()
            for record in records:
                row = {field: record.get(field) for field in fieldnames}
                writer.writerow(row)
    except Exception as exc:
        logger.warning(
            "Failed to write historical data CSV",
            filename=filename,
            error=str(exc),
        )


def _records_to_chart(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    chart: List[Dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        date_value = record.get("date")
        ts = _parse_timestamp(date_value) if date_value else None
        if ts is None and isinstance(date_value, str):
            try:
                ts = datetime.fromisoformat(date_value)
            except ValueError:
                ts = None
        if ts is None:
            ts = now_local()
        chart.append(
            {
                "time": to_local_iso(ts),
                "open": float(record.get("open") or 0),
                "high": float(record.get("high") or 0),
                "low": float(record.get("low") or 0),
                "close": float(record.get("close") or 0),
                "volume": int(record.get("volume") or 0),
            }
        )
    return chart


def _dataframe_to_records(df: Any) -> List[Dict[str, Any]]:
    if df is None or getattr(df, "empty", False):
        return []

    records: List[Dict[str, Any]] = []
    to_dict = getattr(df, "to_dict", None)
    if callable(to_dict):
        try:
            raw_records = to_dict("records")  # type: ignore[call-arg]
            if isinstance(raw_records, list):
                for row in raw_records:
                    if not isinstance(row, dict):
                        continue
                    records.append(
                        {
                            "date": row.get("date") or row.get("time"),
                            "open": row.get("open"),
                            "high": row.get("high"),
                            "low": row.get("low"),
                            "close": row.get("close"),
                            "volume": row.get("volume"),
                        }
                    )
                if records:
                    return records
        except Exception:
            records = []

    columns = list(getattr(df, "columns", []))
    itertuples = getattr(df, "itertuples", None)
    if callable(itertuples):
        try:
            iterator = itertuples(index=False, name=None)
        except TypeError:
            iterator = itertuples()
        for row in iterator:
            row_dict = {}
            for column in columns:
                row_dict[column] = getattr(row, column, None)
            records.append(
                {
                    "date": row_dict.get("date") or row_dict.get("time"),
                    "open": row_dict.get("open"),
                    "high": row_dict.get("high"),
                    "low": row_dict.get("low"),
                    "close": row_dict.get("close"),
                    "volume": row_dict.get("volume"),
                }
            )

    return records


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

        db_manager = core_container.get_db_manager()
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
        records: List[Dict[str, Any]] = []
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

        if not records:
            records = await _fetch_market_history(
                request.symbol,
                interval=interval_key,
                duration_minutes=duration_minutes,
                exchange=request.exchange,
            )

        if not records:
            if cache_df is not None and not getattr(cache_df, "empty", False):
                data_dict = _dataframe_to_records(cache_df)
                if data_dict:
                    return {
                        "success": True,
                        "data": data_dict,
                        "filename": None,
                        "rows": len(data_dict),
                        "source": "database_cache",
                    }
            raise HTTPException(status_code=404, detail="No data found")

        expires_at = now + timedelta(minutes=CACHE_DURATION_MINUTES)
        historical_data_cache[cache_key] = {
            "data": records,
            "timestamp": now,
            "expires_at": expires_at,
        }
        logger.debug(
            "Cached historical data for {}, expires at {}",
            request.symbol,
            expires_at,
        )

        db_manager.save_historical_records(
            request.symbol,
            records,
            bar_size=request.bar_size,
        )

        filename = f"data/{request.symbol}_historical.csv"
        _write_records_to_csv(filename, records)

        return {
            "success": True,
            "data": records,
            "filename": filename,
            "rows": len(records),
            "source": "market_service",
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
        db_manager = core_container.get_db_manager()
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
            chart_data = _records_to_chart(_dataframe_to_records(cache_df))

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
                    unified_chart = _records_to_chart(records)

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

        records = await _fetch_market_history(
            request.symbol,
            interval=interval_key,
            duration_minutes=duration_minutes,
            exchange=request.exchange,
        )

        if not records and cache_df is not None and not getattr(cache_df, "empty", False):
            records = _dataframe_to_records(cache_df)

        if not records:
            raise HTTPException(status_code=404, detail="No chart data found")

        chart_data = _records_to_chart(records)
        expires_at = now + timedelta(minutes=CACHE_DURATION_MINUTES)
        historical_data_cache[cache_key] = {
            "data": chart_data,
            "timestamp": now,
            "expires_at": expires_at,
        }

        db_manager.save_historical_records(
            request.symbol,
            records,
            bar_size=request.bar_size,
        )

        return {
            "success": True,
            "data": chart_data,
            "symbol": request.symbol,
            "duration": request.duration,
            "bar_size": request.bar_size,
        }

    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        logger.error(f"Chart data error: {e}")
        raise HTTPException(status_code=500, detail=str(e) or "Internal error")


@router.get("/api/realtime_data/{symbol}")
async def get_realtime_data(
    symbol: str,
    exchange: str = "SMART",
    user: Optional[str] = Depends(get_current_user),
):
    """获取实时数据"""
    try:
        snapshot = await _fetch_market_snapshot(symbol)
        if snapshot and snapshot.get("last") is not None:
            data = {
                "symbol": symbol,
                "last": snapshot.get("last"),
                "bid": snapshot.get("bid") or 0.0,
                "ask": snapshot.get("ask") or 0.0,
                "volume": snapshot.get("volume") or 0,
                "timestamp": snapshot.get("timestamp") or now_local().isoformat(),
                "source": snapshot.get("source"),
            }
            return {"success": True, "data": data}

        records = await _fetch_market_history(
            symbol,
            interval=None,
            duration_minutes=5,
            exchange=exchange,
        )
        if records:
            last = records[-1]
            data = {
                "symbol": symbol,
                "last": last.get("close"),
                "bid": last.get("close"),
                "ask": last.get("close"),
                "volume": last.get("volume") or 0,
                "timestamp": last.get("date") or now_local().isoformat(),
                "source": "history",
                "is_cached": True,
            }
            return {"success": True, "data": data}

        raise HTTPException(status_code=404, detail="No data available")

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Real-time data error: {}", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/api/current_price")
async def get_current_price(
    request: CurrentPriceRequest,
    user: Optional[str] = Depends(get_current_user),
):
    """获取当前价格 - 轻量级接口"""
    mode = get_data_layer_mode()
    manager = None if is_legacy_mode(mode) else _get_data_layer_manager()

    if CURRENT_PRICE_CACHE_ONLY:
        snapshot = await _fetch_snapshot_from_redis(request.symbol)
        if snapshot and snapshot.get("last") is not None:
            return {
                "success": True,
                "symbol": request.symbol,
                "price": snapshot.get("last"),
                "timestamp": snapshot.get("timestamp") or now_local().isoformat(),
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

    try:
        snapshot = await _fetch_market_snapshot(request.symbol)
        if snapshot and snapshot.get("last") is not None:
            return {
                "success": True,
                "symbol": request.symbol,
                "price": snapshot.get("last"),
                "timestamp": snapshot.get("timestamp") or now_local().isoformat(),
                "source": snapshot.get("source") or "snapshot",
            }

        symbol_upper = (request.symbol or "").upper()
        duration_minutes = 3 if any(
            symbol_upper.startswith(root) for root in INDEX_FUTURE_ROOTS
        ) else 2

        records = await _fetch_market_history(
            request.symbol,
            interval=_map_bar_size_to_interval("1 min"),
            duration_minutes=duration_minutes,
            exchange=request.exchange,
        )

        if records:
            latest = records[-1]
            price = latest.get("close")
            if price is not None:
                return {
                    "success": True,
                    "symbol": request.symbol,
                    "price": price,
                    "timestamp": latest.get("date") or now_local().isoformat(),
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
    except Exception as exc:
        logger.error("Current price error: {}", exc)
        raise HTTPException(status_code=500, detail=str(exc))
