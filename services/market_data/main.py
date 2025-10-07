"""FastAPI application that aggregates market data snapshots from Redis Pub/Sub."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import math
import os
import time
from collections import defaultdict
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, Mapping, Sequence, Optional, List, Tuple

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.ibbot import update_service_health
from src.ibbot.storage.parquet_writer import MarketDataParquetWriter
from src.ibbot.logging import get_logger
from src.ibbot.redis import PubSubListener, get_redis_client
from src.dom_service import get_dom_service
from src.dom_dependencies import dom_config
from src.services.factories import (
    ServiceMetricsWriterProtocol,
    create_service_metrics_writer,
)
from src.utils import NY_TZ, now_local, to_local_iso

PRICE_CHANNEL_TEMPLATE = "pubsub:ib:price:{symbol}"
DOM_CHANNEL_TEMPLATE = "pubsub:ib:dom:{symbol}"
STREAM_KEY = "stream:market-service"
CACHE_PREFIX_ENV = "DATA_LAYER_REDIS_PREFIX"
DEFAULT_CACHE_PREFIX = "market-data"

_BAR_SIZE_TO_INTERVAL = {
    "1 sec": "1s",
    "1 secs": "1s",
    "1 second": "1s",
    "1 seconds": "1s",
    "5 sec": "5s",
    "5 secs": "5s",
    "5 second": "5s",
    "5 seconds": "5s",
    "15 sec": "15s",
    "15 secs": "15s",
    "15 second": "15s",
    "15 seconds": "15s",
    "30 sec": "30s",
    "30 secs": "30s",
    "30 second": "30s",
    "30 seconds": "30s",
    "1 min": "1m",
    "1 mins": "1m",
    "1 minute": "1m",
    "1 minutes": "1m",
    "5 min": "5m",
    "5 mins": "5m",
    "5 minute": "5m",
    "5 minutes": "5m",
    "15 min": "15m",
    "15 mins": "15m",
    "15 minute": "15m",
    "15 minutes": "15m",
    "30 min": "30m",
    "30 mins": "30m",
    "30 minute": "30m",
    "30 minutes": "30m",
    "1 hour": "1h",
    "1 hours": "1h",
    "1 hr": "1h",
    "1 hrs": "1h",
    "1 day": "1d",
    "1 days": "1d",
}

_INTERVAL_TO_SECONDS = {
    "1s": 1,
    "5s": 5,
    "15s": 15,
    "30s": 30,
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "1d": 86400,
}

_IB_BAR_SIZE_BY_INTERVAL = {
    "1s": "1 secs",
    "5s": "5 secs",
    "15s": "15 secs",
    "30s": "30 secs",
    "1m": "1 min",
    "5m": "5 mins",
    "15m": "15 mins",
    "30m": "30 mins",
    "1h": "1 hour",
    "1d": "1 day",
}


dom_service = get_dom_service()
DOM_STOP_TIMEOUT = 2.5
DOM_LOGGER = get_logger("ibbot.services.market_data.dom")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return float(default)
    try:
        return float(raw.split("#", 1)[0].strip()) or float(default)
    except Exception:
        return float(default)


_IB_WAIT_FOR_EXISTING_CONNECT_SEC = max(0.0, _env_float("MARKET_DATA_IB_WAIT_FOR_CONNECT_SEC", 8.0))
_IB_CONNECT_BACKOFF_SECONDS = max(0.0, _env_float("MARKET_DATA_IB_CONNECT_BACKOFF_SEC", 5.0))
_IB_CONNECT_BACKOFF_UNTIL = 0.0

MAX_RETURNED_CANDLES = 1000
_MAX_SOURCE_FETCH = 120_000

_INTERVAL_TO_BAR_SIZES: dict[str, list[str]] = {}
for _bar_size, _interval_name in _BAR_SIZE_TO_INTERVAL.items():
    _INTERVAL_TO_BAR_SIZES.setdefault(_interval_name, []).append(_bar_size)


def _resolve_ib_bar_size(bar_size: str, interval: str) -> str:
    raw = (bar_size or "").strip().lower()
    mapped_interval = _BAR_SIZE_TO_INTERVAL.get(raw)
    if mapped_interval:
        resolved = _IB_BAR_SIZE_BY_INTERVAL.get(mapped_interval)
        if resolved:
            return resolved
    resolved = _IB_BAR_SIZE_BY_INTERVAL.get(interval)
    if resolved:
        return resolved
    return bar_size


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}


IB_FALLBACK_ENABLED = _env_flag("MARKET_DATA_ENABLE_IB_FALLBACK", True)
_IB_CLIENT_LOCK = asyncio.Lock()
_IB_CLIENT: Any | None = None
_IB_CLIENT_UNAVAILABLE = object()
_IB_FETCH_LOCKS: dict[tuple[str, str, str], asyncio.Lock] = {}
_IB_FETCH_LOCK_GUARD = asyncio.Lock()


class ChartDataQuery(BaseModel):
    """Request payload accepted by the chart data endpoint."""

    symbol: str = Field(..., description="Ticker symbol")
    duration: str = Field(..., description="Duration string, e.g. 1 D")
    bar_size: str = Field(..., alias="barSize", description="Bar size, e.g. 1 min")
    exchange: str = Field("SMART", description="Exchange identifier")

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    @field_validator("symbol")
    @classmethod
    def _normalise_symbol(cls, value: str) -> str:
        result = value.strip().upper()
        if not result:
            raise ValueError("symbol must not be empty")
        return result

    @field_validator("duration", "bar_size", "exchange")
    @classmethod
    def _strip_value(cls, value: str) -> str:
        result = value.strip()
        if not result:
            raise ValueError("value must not be empty")
        return result


class DOMStartRequest(BaseModel):
    symbol: str
    num_rows: int = Field(default=5, ge=1, le=30)
    force_restart: bool = False


class DOMStopRequest(BaseModel):
    symbol: str


class DOMConfigModel(BaseModel):
    window: int = dom_config.window
    max_levels: int = dom_config.max_levels
    trend_thresholds: List[float] = Field(
        default_factory=lambda: list(getattr(dom_config, "trend_thresholds", []))
    )


def _parse_duration_to_minutes(duration: str) -> int:
    if not duration:
        return 0
    text = str(duration).strip().lower()
    if not text:
        return 0
    parts = text.split()
    try:
        number = float(parts[0])
    except Exception:
        import re as _re

        match = _re.match(r"(\d+(?:\.\d+)?)([a-zA-Z]+)", text)
        if not match:
            return 0
        number = float(match.group(1))
        unit = match.group(2)
    else:
        unit = parts[1] if len(parts) > 1 else "min"
    unit = unit.strip().lower()
    if unit in {"d", "day", "days"}:
        return int(number * 24 * 60)
    if unit in {"h", "hour", "hours"}:
        return int(number * 60)
    if unit in {"w", "week", "weeks"}:
        return int(number * 7 * 24 * 60)
    if unit in {"m", "min", "mins", "minute", "minutes"}:
        return int(number)
    return int(number)


class BackfillRequest(ChartDataQuery):
    extra_bar_sizes: list[str] | None = Field(
        default=None,
        alias="extraBarSizes",
        description="Additional bar sizes to aggregate after fetching",
    )
    extra_intervals: list[str] | None = Field(
        default=None,
        alias="extraIntervals",
        description="Explicit intervals to aggregate after fetching",
    )


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _candle_sort_key(candle: Mapping[str, Any]) -> int:
    start_ns = _coerce_int(candle.get("interval_start_ns"))
    if start_ns is not None:
        return start_ns
    end_ns = _coerce_int(candle.get("interval_end_ns"))
    if end_ns is not None:
        return end_ns
    timestamp = candle.get("timestamp")
    coerced = _coerce_int(timestamp)
    if coerced is not None:
        return coerced
    return 0


def _normalise_candles(candles: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[int, dict[str, Any]] = {}
    without_keys: list[dict[str, Any]] = []
    for candle in candles:
        record = dict(candle)
        key = _candle_sort_key(record)
        if key:
            # keep the latest observation for each interval key
            seen[key] = record
        else:
            without_keys.append(record)
    ordered = sorted(seen.items(), key=lambda item: item[0])
    return [entry for _, entry in ordered] + without_keys


def _combine_candle_sets(*candle_sets: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    combined: list[dict[str, Any]] = []
    for candles in candle_sets:
        if not candles:
            continue
        combined.extend(dict(c) for c in candles)
    return _normalise_candles(combined)


@dataclass
class CandleCoverage:
    earliest_ns: int | None = None
    latest_ns: int | None = None
    missing_rows: int = 0
    missing_minutes: int = 0

    @property
    def has_gap(self) -> bool:
        return (self.missing_rows > 0) or (self.missing_minutes > 0)


def _calculate_candle_coverage(
    candles: Sequence[Mapping[str, Any]],
    *,
    expected_rows: int,
    duration_minutes: int,
) -> tuple[CandleCoverage, list[dict[str, Any]]]:
    normalised = _normalise_candles(candles)
    earliest_ns = None
    latest_ns = None
    if normalised:
        earliest_ns = _candle_ns(normalised[0], prefer_end=False)
        latest_ns = _candle_ns(normalised[-1], prefer_end=True)

    missing_rows = 0
    if expected_rows > 0:
        missing_rows = max(0, int(expected_rows) - len(normalised))

    missing_minutes = 0
    if duration_minutes > 0:
        if earliest_ns is None:
            missing_minutes = duration_minutes
        else:
            cutoff_dt = datetime.now(NY_TZ) - timedelta(minutes=duration_minutes)
            cutoff_ns = int(cutoff_dt.timestamp() * 1_000_000_000)
            if earliest_ns > cutoff_ns:
                missing_minutes = math.ceil(
                    (earliest_ns - cutoff_ns) / (60 * 1_000_000_000)
                )

    coverage = CandleCoverage(
        earliest_ns=earliest_ns,
        latest_ns=latest_ns,
        missing_rows=missing_rows,
        missing_minutes=missing_minutes,
    )
    return coverage, normalised


def _merge_intervals(intervals: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(intervals, key=lambda item: item[0]):
        if end <= start:
            continue
        if not merged:
            merged.append((start, end))
            continue
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _identify_missing_segments(
    candles: Sequence[Mapping[str, Any]],
    *,
    interval: str,
    duration_minutes: int,
    expected_rows: int,
    coverage: CandleCoverage,
) -> list[tuple[int, int]]:
    interval_seconds = _INTERVAL_TO_SECONDS.get(interval)
    if not interval_seconds:
        return []

    interval_ns = interval_seconds * 1_000_000_000
    normalised = _normalise_candles(candles)
    intervals: list[tuple[int, int]] = []
    for candle in normalised:
        start_ns = _candle_ns(candle, prefer_end=False)
        end_ns = _candle_ns(candle, prefer_end=True)
        if start_ns is None and end_ns is None:
            continue
        if start_ns is None:
            start_ns = end_ns - interval_ns
        if end_ns is None:
            end_ns = start_ns + interval_ns
        if end_ns <= start_ns:
            continue
        intervals.append((start_ns, end_ns))

    intervals = _merge_intervals(intervals)

    if coverage.latest_ns is not None:
        target_end_ns = coverage.latest_ns
    elif intervals:
        target_end_ns = intervals[-1][1]
    else:
        target_end_ns = int(datetime.now(NY_TZ).timestamp() * 1_000_000_000)

    desired_start_candidates: list[int] = []
    if duration_minutes > 0:
        desired_start_candidates.append(
            target_end_ns - duration_minutes * 60 * 1_000_000_000
        )
    if expected_rows > 0:
        desired_start_candidates.append(
            target_end_ns - expected_rows * interval_ns
        )

    if not desired_start_candidates:
        return []

    desired_start_ns = min(desired_start_candidates)
    desired_start_ns = min(desired_start_ns, target_end_ns - interval_ns)
    if desired_start_ns < 0:
        desired_start_ns = 0
    if desired_start_ns >= target_end_ns:
        return []

    segments: list[tuple[int, int]] = []
    cursor = desired_start_ns
    for start_ns, end_ns in intervals:
        if end_ns <= desired_start_ns:
            continue
        if start_ns >= target_end_ns:
            break
        if start_ns > cursor:
            gap_end = min(start_ns, target_end_ns)
            if gap_end > cursor:
                segments.append((cursor, gap_end))
        cursor = max(cursor, min(end_ns, target_end_ns))
        if cursor >= target_end_ns:
            break

    if cursor < target_end_ns:
        segments.append((cursor, target_end_ns))

    return [segment for segment in segments if segment[1] > segment[0]]


def _candidate_source_intervals(target_interval: str) -> list[str]:
    if target_interval not in _INTERVAL_TO_SECONDS:
        return [target_interval]
    target_seconds = _INTERVAL_TO_SECONDS[target_interval]
    divisors = [
        interval
        for interval, seconds in _INTERVAL_TO_SECONDS.items()
        if seconds < target_seconds and target_seconds % seconds == 0
    ]
    divisors.sort(key=lambda key: _INTERVAL_TO_SECONDS[key], reverse=True)
    return [target_interval, *divisors]


def _aggregate_candles(
    candles: Sequence[Mapping[str, Any]],
    *,
    target_interval: str,
    source_interval: str,
) -> list[dict[str, Any]]:
    if target_interval == source_interval:
        return [dict(candle) for candle in candles]

    target_seconds = _INTERVAL_TO_SECONDS.get(target_interval)
    source_seconds = _INTERVAL_TO_SECONDS.get(source_interval)
    if not target_seconds or not source_seconds:
        return []
    if target_seconds % source_seconds != 0:
        return []

    sorted_candles = _normalise_candles(candles)
    if not sorted_candles:
        return []

    bucket_size_ns = target_seconds * 1_000_000_000
    source_duration_ns = source_seconds * 1_000_000_000
    aggregated: list[dict[str, Any]] = []
    current_bucket: dict[str, Any] | None = None

    for candle in sorted_candles:
        start_ns = _coerce_int(candle.get("interval_start_ns"))
        end_ns = _coerce_int(candle.get("interval_end_ns"))
        if start_ns is None and end_ns is None:
            continue
        if start_ns is None and end_ns is not None:
            start_ns = end_ns - source_duration_ns
        if end_ns is None and start_ns is not None:
            end_ns = start_ns + source_duration_ns
        if start_ns is None or end_ns is None:
            continue

        bucket_start_ns = (start_ns // bucket_size_ns) * bucket_size_ns
        bucket_end_ns = bucket_start_ns + bucket_size_ns

        open_price = _coerce_float(candle.get("open"))
        high_price = _coerce_float(candle.get("high"))
        low_price = _coerce_float(candle.get("low"))
        close_price = _coerce_float(candle.get("close"))
        volume = _coerce_int(candle.get("volume")) or 0

        if current_bucket is None or current_bucket["interval_start_ns"] != bucket_start_ns:
            if current_bucket is not None:
                aggregated.append(current_bucket)
            current_bucket = {
                "interval_start_ns": bucket_start_ns,
                "interval_end_ns": bucket_end_ns,
                "open": open_price if open_price is not None else close_price,
                "high": high_price if high_price is not None else open_price,
                "low": low_price if low_price is not None else open_price,
                "close": close_price if close_price is not None else open_price,
                "volume": volume,
            }
            continue

        if high_price is not None:
            current_bucket_high = _coerce_float(current_bucket.get("high"))
            if current_bucket_high is None or high_price > current_bucket_high:
                current_bucket["high"] = high_price
        if low_price is not None:
            current_bucket_low = _coerce_float(current_bucket.get("low"))
            if current_bucket_low is None or low_price < current_bucket_low:
                current_bucket["low"] = low_price
        if close_price is not None:
            current_bucket["close"] = close_price
            if current_bucket.get("open") is None:
                current_bucket["open"] = close_price
        current_bucket["volume"] = (current_bucket.get("volume") or 0) + volume

    if current_bucket is not None:
        aggregated.append(current_bucket)

    return aggregated


def _estimate_result_rows(duration_minutes: int, target_seconds: int) -> int:
    if duration_minutes <= 0 or target_seconds <= 0:
        return MAX_RETURNED_CANDLES
    estimated = math.ceil((duration_minutes * 60) / target_seconds)
    return max(1, min(estimated, MAX_RETURNED_CANDLES))


def _estimate_source_limit(
    *,
    duration_minutes: int,
    target_seconds: int,
    source_seconds: int,
    target_rows: int,
) -> int:
    if source_seconds <= 0:
        return target_rows
    ratio = math.ceil(target_seconds / source_seconds)
    limit = max(target_rows * max(ratio, 1), target_rows)
    if duration_minutes > 0:
        duration_needed = math.ceil((duration_minutes * 60) / source_seconds)
        limit = max(limit, duration_needed)
    return min(max(limit, target_rows), _MAX_SOURCE_FETCH)


def _coerce_datetime_any(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=NY_TZ)
        except Exception:
            return None
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            for pattern in ("%Y%m%d %H:%M:%S", "%Y%m%d %H:%M", "%Y-%m-%d %H:%M:%S"):
                try:
                    candidate = datetime.strptime(text, pattern)
                except ValueError:
                    continue
                dt = candidate.replace(tzinfo=NY_TZ)
                break
            else:
                return None
    else:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=NY_TZ)
    else:
        dt = dt.astimezone(NY_TZ)
    return dt


def _bar_timestamp(bar: Mapping[str, Any]) -> datetime | None:
    for key in ("timestamp", "time", "date", "datetime"):
        candidate = bar.get(key)
        dt = _coerce_datetime_any(candidate)
        if dt is not None:
            return dt
    return None


def _bars_to_candles(
    bars: Sequence[Mapping[str, Any]],
    *,
    symbol: str,
    interval: str,
) -> list[dict[str, Any]]:
    seconds = _INTERVAL_TO_SECONDS.get(interval)
    if not seconds:
        return []
    period_ns = seconds * 1_000_000_000
    candles: list[dict[str, Any]] = []
    for bar in bars:
        if not isinstance(bar, Mapping):
            continue
        dt = _bar_timestamp(bar)
        if dt is None:
            continue
        end_ns = int(dt.timestamp() * 1_000_000_000)
        start_ns = end_ns - period_ns

        open_price = _coerce_float(bar.get("open"))
        high_price = _coerce_float(bar.get("high"))
        low_price = _coerce_float(bar.get("low"))
        close_price = _coerce_float(bar.get("close"))
        volume_value = _coerce_float(bar.get("volume"))
        count_value = _coerce_int(bar.get("count"))

        if open_price is None and close_price is not None:
            open_price = close_price
        if close_price is None and open_price is not None:
            close_price = open_price
        if high_price is None:
            high_price = max(
                [price for price in (open_price, close_price) if price is not None],
                default=open_price,
            )
        if low_price is None:
            low_price = min(
                [price for price in (open_price, close_price) if price is not None],
                default=open_price,
            )

        candle = {
            "symbol": symbol.upper(),
            "interval": interval,
            "interval_start_ns": start_ns,
            "interval_end_ns": end_ns,
            "open": open_price if open_price is not None else 0.0,
            "high": high_price if high_price is not None else (open_price or 0.0),
            "low": low_price if low_price is not None else (open_price or 0.0),
            "close": close_price if close_price is not None else (open_price or 0.0),
            "volume": volume_value if volume_value is not None else 0.0,
        }
        if count_value is not None:
            candle["count"] = count_value
        candles.append(candle)

    candles.sort(key=lambda item: item.get("interval_end_ns", 0))
    return candles


def _ns_to_iso(ns: int | None) -> str | None:
    if ns is None:
        return None
    try:
        dt = datetime.fromtimestamp(ns / 1_000_000_000, tz=NY_TZ)
    except Exception:
        return None
    return dt.isoformat()


def _candle_ns(
    candle: Mapping[str, Any] | None,
    *,
    prefer_end: bool = True,
) -> int | None:
    if not candle:
        return None

    ordered_fields = ["interval_end_ns", "interval_start_ns", "timestamp_ns"]
    if not prefer_end:
        ordered_fields = ["interval_start_ns", "interval_end_ns", "timestamp_ns"]

    for field in ordered_fields:
        candidate = _coerce_int(candle.get(field))
        if candidate is not None:
            return candidate

    timestamp_value = candle.get("timestamp")
    dt = _coerce_datetime_any(timestamp_value)
    if dt is not None:
        try:
            return int(dt.timestamp() * 1_000_000_000)
        except Exception:
            return None
    return None


def _decode_redis_candle(entry: Any) -> dict[str, Any] | None:
    if entry is None:
        return None
    if isinstance(entry, bytes):
        entry = entry.decode("utf-8", "ignore")
    if isinstance(entry, str):
        text = entry.strip()
        if not text:
            return None
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            return None
    elif isinstance(entry, Mapping):
        decoded = dict(entry)
    else:
        return None
    return decoded if isinstance(decoded, dict) else None


async def _summarise_redis_series(redis_client: Any, key: str) -> dict[str, Any]:
    summary: dict[str, Any] = {"count": 0, "oldest": None, "newest": None}
    try:
        count = await redis_client.llen(key)
    except Exception:
        return summary

    summary["count"] = int(count or 0)
    if summary["count"] <= 0:
        return summary

    try:
        oldest_raw, newest_raw = await asyncio.gather(
            redis_client.lindex(key, 0),
            redis_client.lindex(key, -1),
        )
    except Exception:
        return summary

    oldest_candle = _decode_redis_candle(oldest_raw)
    newest_candle = _decode_redis_candle(newest_raw)
    summary["oldest"] = _ns_to_iso(_candle_ns(oldest_candle, prefer_end=False))
    summary["newest"] = _ns_to_iso(_candle_ns(newest_candle, prefer_end=True))
    return summary


def _list_parquet_segments(
    writer: MarketDataParquetWriter | None,
    *,
    symbol: str,
    interval: str,
) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    if writer is None:
        return segments

    base_dir = getattr(writer, "base_dir", None)
    if not base_dir:
        return segments

    directory = Path(base_dir) / "candles" / interval / symbol.upper()
    if not directory.exists():
        return segments

    try:
        from pyarrow import parquet as _parquet  # type: ignore
    except Exception:  # pragma: no cover - optional dependency
        _parquet = None

    for file_path in sorted(directory.glob("*.parquet")):
        entry: dict[str, Any] = {
            "file": file_path.name,
            "size": file_path.stat().st_size,
            "path": str(file_path),
        }
        if _parquet is not None:
            try:
                table = _parquet.read_table(
                    file_path,
                    columns=["interval_start_ns", "interval_end_ns"],
                )
                entry["rows"] = table.num_rows
                if table.num_rows:
                    start_value = table.column("interval_start_ns")[0].as_py()
                    end_value = table.column("interval_end_ns")[table.num_rows - 1].as_py()
                    entry["start"] = _ns_to_iso(_coerce_int(start_value))
                    entry["end"] = _ns_to_iso(_coerce_int(end_value))
            except Exception:
                entry.setdefault("rows", None)
        entry["modified"] = _ns_to_iso(int(file_path.stat().st_mtime * 1_000_000_000))
        segments.append(entry)

    return segments


async def _ensure_ib_client(logger: Any) -> Any | None:
    global _IB_CLIENT
    if _IB_CLIENT is _IB_CLIENT_UNAVAILABLE:
        return None
    if _IB_CLIENT is not None:
        return _IB_CLIENT
    async with _IB_CLIENT_LOCK:
        if _IB_CLIENT is None:
            try:
                from src.ib_client_registry import get_client  # type: ignore
            except Exception:
                if logger:
                    logger.warning(
                        "IBKR client unavailable; disabling fallback",
                        exc_info=True,
                    )
                _IB_CLIENT = _IB_CLIENT_UNAVAILABLE
                return None
            try:
                _IB_CLIENT = await get_client("secondary")
            except Exception:
                if logger:
                    logger.warning(
                        "Failed to obtain shared IBKR client; disabling fallback",
                        exc_info=True,
                    )
                _IB_CLIENT = _IB_CLIENT_UNAVAILABLE
                return None
    return None if _IB_CLIENT is _IB_CLIENT_UNAVAILABLE else _IB_CLIENT


async def _ensure_ib_connected(client: Any, logger: Any) -> bool:
    ensure_fn = getattr(client, "ensure_connection", None)
    if callable(ensure_fn):
        try:
            await ensure_fn(reason="market-data-fallback")
            return True
        except Exception:
            if logger:
                logger.warning("IBKR ensure_connection failed", exc_info=True)
            return False

    try:
        if getattr(client, "connected", False):
            return True
    except Exception:
        pass

    manager = getattr(client, "connection_manager", None)
    if manager is not None:
        try:
            if manager.is_connected():
                return True
        except Exception:
            pass

        try:
            if manager.is_connecting():
                waited = await asyncio.to_thread(
                    manager.wait_for_connection,
                    _IB_WAIT_FOR_EXISTING_CONNECT_SEC,
                )
                if waited:
                    return True
        except Exception:
            if logger:
                logger.debug(
                    "Failed while waiting for existing IBKR connection attempt",
                    exc_info=True,
                )

    now = time.monotonic()
    global _IB_CONNECT_BACKOFF_UNTIL
    if now < _IB_CONNECT_BACKOFF_UNTIL:
        if logger:
            logger.debug(
                "Skipping IBKR fallback connect due to backoff",
                remaining=round(_IB_CONNECT_BACKOFF_UNTIL - now, 2),
            )
        return False

    connect_fn = getattr(client, "connect", None)
    if not callable(connect_fn):
        if logger:
            logger.warning("IBKR client missing connect() method; cannot establish connection")
        return False

    try:
        result = await asyncio.to_thread(connect_fn)
    except Exception:
        if logger:
            logger.warning("IBKR connect() call failed", exc_info=True)
        _IB_CONNECT_BACKOFF_UNTIL = now + _IB_CONNECT_BACKOFF_SECONDS
        return False

    if not result:
        if logger:
            logger.warning("IBKR connect() returned False")
        _IB_CONNECT_BACKOFF_UNTIL = now + _IB_CONNECT_BACKOFF_SECONDS
        return False

    _IB_CONNECT_BACKOFF_UNTIL = 0.0
    return True


@asynccontextmanager
async def _exclusive_ib_fetch(key: tuple[str, str, str]):
    async with _IB_FETCH_LOCK_GUARD:
        lock = _IB_FETCH_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _IB_FETCH_LOCKS[key] = lock
    await lock.acquire()
    try:
        yield
    finally:
        lock.release()
        async with _IB_FETCH_LOCK_GUARD:
            stored = _IB_FETCH_LOCKS.get(key)
            if stored is lock and not lock.locked():
                _IB_FETCH_LOCKS.pop(key, None)


async def _fetch_candles_from_ib(
    query: ChartDataQuery,
    *,
    interval: str,
    persist: Callable[[Sequence[Mapping[str, Any]]], Awaitable[None]],
    logger: Any,
    missing_segments: Sequence[tuple[int, int]] | None = None,
) -> list[dict[str, Any]]:
    if not IB_FALLBACK_ENABLED:
        return []

    client = await _ensure_ib_client(logger)
    if client is None:
        if logger:
            logger.info(
                "IB fallback disabled; IBKR client unavailable for %s interval=%s",
                query.symbol,
                interval,
            )
        return []

    lock_key = (query.symbol.upper(), interval, query.duration)
    async with _exclusive_ib_fetch(lock_key):
        try:
            if getattr(client, "connected", False):
                connected = True
            else:
                connected = await _ensure_ib_connected(client, logger)
        except Exception:
            connected = await _ensure_ib_connected(client, logger)

        if not connected:
            if logger:
                logger.warning(
                    "IB fallback skipped; IBKR client not connected for %s interval=%s",
                    query.symbol,
                    interval,
                )
            return []

        bar_size = _resolve_ib_bar_size(query.bar_size, interval)

        interval_seconds = _INTERVAL_TO_SECONDS.get(interval, 0)
        requests: list[tuple[datetime | None, str]] = []
        if missing_segments and interval_seconds > 0:
            for start_ns, end_ns in missing_segments:
                if end_ns <= start_ns:
                    continue
                duration_seconds = math.ceil((end_ns - start_ns) / 1_000_000_000)
                duration_seconds = max(duration_seconds, interval_seconds)
                duration_seconds += interval_seconds
                try:
                    end_dt = datetime.fromtimestamp(end_ns / 1_000_000_000, tz=NY_TZ)
                except Exception:
                    end_dt = None
                requests.append((end_dt, f"{int(duration_seconds)} S"))

        if not requests:
            requests.append((None, query.duration))

        combined_records: list[Mapping[str, Any]] = []
        for end_dt, duration_override in requests:
            duration_value = duration_override or query.duration
            if not duration_value:
                duration_value = "1 D"
            try:
                data_frame = await client.get_historical_data(
                    query.symbol,
                    duration=duration_value,
                    bar_size=bar_size,
                    exchange=query.exchange,
                    end_datetime=end_dt,
                )
            except Exception:
                if logger:
                    logger.warning(
                        "IBKR historical request failed for %s interval=%s",
                        query.symbol,
                        interval,
                        exc_info=True,
                    )
                return []

            if data_frame is None:
                continue

            try:
                records = data_frame.to_dict("records")
            except Exception:
                if logger:
                    logger.warning(
                        "Failed to serialise IBKR historical data for %s",
                        query.symbol,
                        exc_info=True,
                    )
                return []

            if not records:
                continue

            combined_records.extend(records)

        if not combined_records:
            if logger:
                logger.info(
                    "IB fallback returned empty records for %s interval=%s",
                    query.symbol,
                    interval,
                )
            return []

        candles = _bars_to_candles(combined_records, symbol=query.symbol, interval=interval)
        candles = _normalise_candles(candles)
        if not candles:
            if logger:
                logger.info(
                    "IB fallback produced no candles for %s interval=%s",
                    query.symbol,
                    interval,
                )
            return []

        try:
            await persist(candles)
        except Exception:
            if logger:
                logger.warning(
                    "Failed to persist IBKR fallback candles for %s interval=%s",
                    query.symbol,
                    interval,
                    exc_info=True,
                )
            return []

        if logger:
            logger.info(
                "IB fallback stored %s candles for %s interval=%s",
                len(candles),
                query.symbol,
                interval,
            )

        return list(candles)


def _filter_candles_by_duration(candles: list[dict[str, Any]], duration_minutes: int) -> list[dict[str, Any]]:
    if duration_minutes <= 0:
        return list(candles)
    # Filter relative to the market (NY) clock so comparisons with candle timestamps stay consistent
    cutoff = datetime.now(NY_TZ) - timedelta(minutes=duration_minutes)
    filtered: list[dict[str, Any]] = []
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


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _safe_int(value: Any) -> int:
    try:
        return int(float(value))
    except Exception:
        return 0


def _format_candles_for_response(candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    formatted: list[dict[str, Any]] = []
    for candle in candles:
        ts_ns = candle.get("interval_end_ns") or candle.get("interval_start_ns")
        timestamp = None
        if ts_ns is not None:
            try:
                timestamp = datetime.fromtimestamp(int(ts_ns) / 1_000_000_000, tz=NY_TZ)
            except Exception:
                timestamp = None
        time_value = to_local_iso(timestamp) if timestamp else to_local_iso(now_local())
        formatted.append(
            {
                "time": time_value or now_local().isoformat(),
                "open": _safe_float(candle.get("open")),
                "high": _safe_float(candle.get("high")),
                "low": _safe_float(candle.get("low")),
                "close": _safe_float(candle.get("close")),
                "volume": _safe_int(candle.get("volume")),
            }
        )
    return formatted


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None
    return None


class WebSocketManager:
    """Manage active WebSocket connections and broadcast updates."""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, initial_payload: Mapping[str, Any]) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)
        await websocket.send_json(initial_payload)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(websocket)

    async def broadcast(self, payload: Mapping[str, Any]) -> None:
        async with self._lock:
            connections = list(self._connections)
        for websocket in connections:
            try:
                await websocket.send_json(payload)
            except WebSocketDisconnect:
                await self.disconnect(websocket)
            except RuntimeError:
                await self.disconnect(websocket)


MetricsRepositoryFactory = Callable[
    [],
    Awaitable[ServiceMetricsWriterProtocol | None]
    | ServiceMetricsWriterProtocol
    | None,
]


@dataclass(slots=True)
class MarketDataRuntime:
    """Aggregate market snapshots and expose them over HTTP/WebSocket."""

    redis_factory: Callable[[], Awaitable[Any]] = get_redis_client
    pubsub_listener: PubSubListener | None = None
    subscriptions: Sequence[str] = ()
    logger_name: str = "ibbot.services.market_data"
    stream_key: str = STREAM_KEY
    metrics_interval: float = 5.0
    service_name: str = "market-data-service"
    health_service_name: str = "market-data"
    health_key: str | None = None
    stream_maxlen: int = 5000
    metrics_repository_factory: MetricsRepositoryFactory | None = None

    _loop: asyncio.AbstractEventLoop | None = field(default=None, init=False, repr=False)
    _redis: Any = field(default=None, init=False, repr=False)
    _pubsub: PubSubListener | None = field(default=None, init=False, repr=False)
    _ws_manager: WebSocketManager = field(default_factory=WebSocketManager, init=False, repr=False)
    _snapshots: Dict[str, Dict[str, Any]] = field(default_factory=dict, init=False, repr=False)
    _updated_at: Dict[str, datetime] = field(default_factory=dict, init=False, repr=False)
    _metrics_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _stop_metrics: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _metrics_counts: Dict[str, float] = field(default_factory=lambda: defaultdict(float), init=False, repr=False)
    _metrics_latency: Dict[str, float] = field(default_factory=lambda: defaultdict(float), init=False, repr=False)
    _metrics_latency_max: Dict[str, float] = field(default_factory=lambda: defaultdict(float), init=False, repr=False)
    _last_metrics_at: datetime = field(default_factory=_now, init=False, repr=False)
    _logger: Any = field(default=None, init=False, repr=False)
    _metrics_repository: ServiceMetricsWriterProtocol | None = field(
        default=None, init=False, repr=False
    )
    _metrics_repository_close: Callable[[], Awaitable[None]] | None = field(
        default=None, init=False, repr=False
    )

    async def start(self) -> None:
        self._logger = get_logger(self.logger_name)
        self._loop = asyncio.get_running_loop()
        try:
            await self._initialize_metrics_repository()
            self._redis = await self.redis_factory()
        except Exception:
            await self._close_metrics_repository()
            self._redis = None
            self._loop = None
            raise
        await self._publish_health(status="starting", healthy=False)
        self._pubsub = self.pubsub_listener or PubSubListener(redis_factory=self.redis_factory)
        await self._register_channels()
        await self._pubsub.start()
        self._stop_metrics.clear()
        self._last_metrics_at = _now()
        self._metrics_task = asyncio.create_task(self._metrics_loop(), name="market-metrics")
        await self._publish_health(status="running", healthy=True)

    async def stop(self) -> None:
        if self._metrics_task is not None:
            self._stop_metrics.set()
            self._metrics_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._metrics_task
            self._metrics_task = None
        if self._pubsub is not None:
            await self._pubsub.stop()
            self._pubsub = None
        await self._publish_health(status="offline", healthy=False)
        self._redis = None
        self._loop = None
        await self._close_metrics_repository()

    async def _initialize_metrics_repository(self) -> None:
        if self._metrics_repository is not None:
            return
        factory = self.metrics_repository_factory
        if factory is None:
            return
        repository = factory()
        if inspect.isawaitable(repository):
            repository = await repository
        self._metrics_repository = repository

        close_callable = getattr(repository, "close", None)

        if close_callable is not None and callable(close_callable):

            async def _close_repo() -> None:
                result = close_callable()
                if inspect.isawaitable(result):
                    await result

            self._metrics_repository_close = _close_repo
        else:
            session = getattr(repository, "_session", None)
            close_session = getattr(session, "close", None) if session is not None else None
            if callable(close_session):

                async def _close_session() -> None:
                    result = close_session()
                    if inspect.isawaitable(result):
                        await result

                self._metrics_repository_close = _close_session
            else:
                self._metrics_repository_close = None

    async def _close_metrics_repository(self) -> None:
        closer = self._metrics_repository_close
        self._metrics_repository_close = None
        repository = self._metrics_repository
        self._metrics_repository = None
        if closer is None:
            return
        try:
            await closer()
        except Exception:
            if self._logger:
                self._logger.warning("Failed to close service metrics repository", exc_info=True)

    async def _register_channels(self) -> None:
        if self._pubsub is None:
            return
        for symbol in self.subscriptions:
            price_channel = PRICE_CHANNEL_TEMPLATE.format(symbol=symbol)
            dom_channel = DOM_CHANNEL_TEMPLATE.format(symbol=symbol)
            self._pubsub.register_callback(price_channel, self._handle_price_message)
            self._pubsub.register_callback(dom_channel, self._handle_dom_message)

    async def _handle_price_message(self, channel: str, payload: str) -> None:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            self._logger.warning("Invalid price payload on channel %s", channel)
            return
        symbol = str(data.get("symbol")) if data.get("symbol") else None
        if not symbol:
            self._logger.warning("Price payload missing symbol on channel %s", channel)
            return
        timestamp = _parse_timestamp(data.get("timestamp")) or _now()
        await self._update_snapshot(symbol, {"price": data}, timestamp, message_type="price")

    async def _handle_dom_message(self, channel: str, payload: str) -> None:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            self._logger.warning("Invalid DOM payload on channel %s", channel)
            return
        symbol = str(data.get("symbol")) if data.get("symbol") else None
        if not symbol:
            self._logger.warning("DOM payload missing symbol on channel %s", channel)
            return
        timestamp = _parse_timestamp(data.get("timestamp")) or _now()
        await self._update_snapshot(symbol, {"dom": data}, timestamp, message_type="dom")

    async def _update_snapshot(
        self,
        symbol: str,
        components: Mapping[str, Any],
        timestamp: datetime,
        *,
        message_type: str,
    ) -> None:
        existing = self._snapshots.setdefault(symbol, {"symbol": symbol, "price": None, "dom": None})
        existing.update({key: value for key, value in components.items()})
        self._updated_at[symbol] = timestamp
        latency = max((_now() - timestamp).total_seconds(), 0.0)
        await self._append_stream(symbol, message_type, components)
        await self._broadcast(symbol, message_type)
        self._record_metrics(message_type, latency)

    async def _append_stream(self, symbol: str, message_type: str, components: Mapping[str, Any]) -> None:
        if self._redis is None:
            return
        payload = json.dumps({"symbol": symbol, "type": message_type, **components}, separators=(",", ":"))
        fields = {
            "symbol": symbol,
            "type": message_type,
            "payload": payload,
        }
        try:
            await self._redis.xadd(
                self.stream_key,
                fields,
                maxlen=self.stream_maxlen,
                approximate=True,
            )
        except Exception:
            self._logger.warning(
                "Failed to append %s message for %s to market stream",
                message_type,
                symbol,
                exc_info=True,
            )

    async def _broadcast(self, symbol: str, message_type: str) -> None:
        snapshot = self.get_snapshot(symbol)
        if snapshot is None:
            return
        payload = {"symbol": symbol, "type": message_type, "data": snapshot}
        await self._ws_manager.broadcast(payload)

    def _record_metrics(self, message_type: str, latency: float) -> None:
        self._metrics_counts[message_type] += 1
        self._metrics_latency[message_type] += latency
        self._metrics_latency_max[message_type] = max(self._metrics_latency_max[message_type], latency)

    async def _metrics_loop(self) -> None:
        try:
            while True:
                try:
                    await asyncio.wait_for(self._stop_metrics.wait(), timeout=self.metrics_interval)
                    break
                except asyncio.TimeoutError:
                    await self._flush_metrics()
        finally:
            await self._flush_metrics(final=True)

    async def _flush_metrics(self, *, final: bool = False) -> None:
        now = _now()
        elapsed = (now - self._last_metrics_at).total_seconds()
        if elapsed <= 0:
            elapsed = self.metrics_interval
        self._last_metrics_at = now
        repository = self._metrics_repository
        entries: list[dict[str, Any]] = []
        health_metrics: dict[str, Any] = {"interval": elapsed, "final": final}
        for metric, count in list(self._metrics_counts.items()):
            if count <= 0 and not final:
                continue
            latency_total = self._metrics_latency.get(metric, 0.0)
            max_latency = self._metrics_latency_max.get(metric, 0.0)
            avg_latency = latency_total / count if count else 0.0
            throughput = count / elapsed if elapsed > 0 else 0.0
            entries.append(
                {
                    "service": self.service_name,
                    "metric": f"{metric}-throughput",
                    "value": throughput,
                    "details": {"updates": int(count), "interval": float(elapsed)},
                }
            )
            entries.append(
                {
                    "service": self.service_name,
                    "metric": f"{metric}-latency",
                    "value": avg_latency,
                    "details": {
                        "updates": int(count),
                        "max_latency": max_latency,
                    },
                }
            )
            health_metrics[f"{metric}_updates"] = int(count)
            health_metrics[f"{metric}_throughput"] = throughput
            health_metrics[f"{metric}_latency_avg_ms"] = round(avg_latency * 1000, 3)
            health_metrics[f"{metric}_latency_max_ms"] = round(max_latency * 1000, 3)
            self._metrics_counts[metric] = 0.0
            self._metrics_latency[metric] = 0.0
            self._metrics_latency_max[metric] = 0.0
        if entries and repository is not None:
            try:
                await repository.record_metrics(entries)
            except Exception:
                if self._logger:
                    self._logger.warning("Failed to persist service metrics", exc_info=True)
        await self._publish_health(status="running", healthy=True, metrics=health_metrics)

    async def _publish_health(
        self,
        *,
        status: str | None = None,
        healthy: bool | None = None,
        metrics: Mapping[str, Any] | None = None,
    ) -> None:
        if self._redis is None:
            return
        try:
            await update_service_health(
                self._redis,
                self.health_service_name,
                status=status,
                healthy=healthy,
                metrics=metrics,
                key=self.health_key,
                logger=self._logger,
            )
        except Exception:
            if self._logger:
                self._logger.warning("Failed to publish market data health", exc_info=True)

    def get_snapshot(self, symbol: str) -> Dict[str, Any] | None:
        symbol = symbol.upper()
        if symbol not in self._snapshots:
            return None
        snapshot = dict(self._snapshots[symbol])
        updated = self._updated_at.get(symbol)
        snapshot["symbol"] = symbol
        snapshot["updated_at"] = updated.isoformat() if updated else None
        return snapshot

    def get_all_snapshots(self) -> list[Dict[str, Any]]:
        result: list[Dict[str, Any]] = []
        for symbol in sorted(self._snapshots.keys()):
            entry = self.get_snapshot(symbol)
            if entry is not None:
                result.append(entry)
        return result

    def emit_price(self, symbol: str, data: Mapping[str, Any]) -> Any:
        payload = json.dumps({**data, "symbol": symbol}, separators=(",", ":"))
        channel = PRICE_CHANNEL_TEMPLATE.format(symbol=symbol)
        return self._schedule(self._handle_price_message(channel, payload))

    def emit_dom(self, symbol: str, data: Mapping[str, Any]) -> Any:
        payload = json.dumps({**data, "symbol": symbol}, separators=(",", ":"))
        channel = DOM_CHANNEL_TEMPLATE.format(symbol=symbol)
        return self._schedule(self._handle_dom_message(channel, payload))

    def _schedule(self, coro: Awaitable[Any]) -> Any:
        loop = self._loop
        if loop is None:
            raise RuntimeError("Service is not running")
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            return asyncio.create_task(coro)
        return asyncio.run_coroutine_threadsafe(coro, loop)


def create_app(*, runtime: MarketDataRuntime | None = None) -> FastAPI:
    runtime = runtime or MarketDataRuntime()

    if runtime.metrics_repository_factory is None:
        runtime.metrics_repository_factory = create_service_metrics_writer

    cache_prefix = os.getenv(CACHE_PREFIX_ENV, DEFAULT_CACHE_PREFIX).strip() or DEFAULT_CACHE_PREFIX
    cache_prefix = cache_prefix.rstrip(":")

    parquet_writer: MarketDataParquetWriter | None = None
    parquet_writer_failed = False

    def _ensure_parquet_writer(logger: Any) -> MarketDataParquetWriter | None:
        nonlocal parquet_writer, parquet_writer_failed
        if parquet_writer_failed:
            return None
        if parquet_writer is not None:
            return parquet_writer
        storage_path = (
            os.getenv("DATA_LAYER_PARQUET_PATH")
            or os.getenv("MARKET_DATA_STORAGE_PATH")
            or "data/market_data"
        )
        if isinstance(storage_path, str):
            storage_path = storage_path.strip() or "data/market_data"
        compression = os.getenv("MARKET_DATA_PARQUET_COMPRESSION") or "snappy"
        if isinstance(compression, str):
            compression = compression.strip() or "snappy"
        try:
            parquet_writer = MarketDataParquetWriter(base_dir=storage_path, compression=compression)
            return parquet_writer
        except Exception as exc:
            if logger:
                logger.warning(
                    "Failed to initialise Parquet fallback for chart data: %s",
                    exc,
                )
            parquet_writer = None
            parquet_writer_failed = True
            return None

    async def _load_candles_from_storage(
        query: ChartDataQuery,
        *,
        interval: str,
        limit: int | None,
        logger: Any,
    ) -> list[dict[str, Any]]:
        writer = _ensure_parquet_writer(logger)
        if writer is None:
            return []
        try:
            records = writer.read_candles(query.symbol, interval, limit=limit)
        except Exception as exc:
            if logger:
                logger.warning(
                    "Failed to read Parquet candles for %s interval=%s: %s",
                    query.symbol,
                    interval,
                    exc,
                )
            return []
        result: list[dict[str, Any]] = []
        for record in records or []:
            if isinstance(record, Mapping):
                result.append(dict(record))
        return result

    async def _store_candles_in_cache(
        *,
        redis_client: Any,
        key: str,
        candles: list[dict[str, Any]],
        limit: int | None,
        logger: Any,
    ) -> None:
        if not candles:
            return
        pipeline_factory = getattr(redis_client, "pipeline", None)
        if pipeline_factory is None:
            return
        try:
            pipe = pipeline_factory()
        except Exception:
            return
        try:
            pipe.delete(key)
            for candle in candles:
                try:
                    payload = json.dumps(candle, separators=(",", ":"))
                except (TypeError, ValueError):
                    continue
                pipe.rpush(key, payload)
            if limit and limit > 0:
                pipe.ltrim(key, -int(limit), -1)
            await pipe.execute()
        except Exception:
            if logger:
                logger.warning(
                    "Failed to populate Redis cache with Parquet candles for %s",
                    key,
                    exc_info=True,
                )

    async def _persist_candles_for_interval(
        *,
        symbol: str,
        interval: str,
        candles: Sequence[Mapping[str, Any]],
        redis_client: Any,
        logger: Any,
        cache_limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if not candles:
            return []

        normalised = _normalise_candles(candles)
        if not normalised:
            return []

        writer = _ensure_parquet_writer(logger)
        if writer is not None:
            try:
                for candle in normalised:
                    writer.append_candle(symbol, interval, candle)
            except Exception:
                if logger:
                    logger.warning(
                        "Failed to append Parquet candles for %s interval=%s",
                        symbol,
                        interval,
                        exc_info=True,
                    )

        key = _series_key(symbol, interval)
        try:
            existing = await _fetch_cached_candles(
                redis_client=redis_client,
                key=key,
                limit=cache_limit,
                logger=logger,
            )
        except HTTPException:
            raise
        except Exception:
            existing = []

        combined = _combine_candle_sets(existing, normalised)
        limit = cache_limit or MAX_RETURNED_CANDLES
        if limit and limit > 0:
            limit = min(int(limit), _MAX_SOURCE_FETCH)
            combined = combined[-limit:]
        else:
            limit = min(MAX_RETURNED_CANDLES, _MAX_SOURCE_FETCH)

        try:
            await _store_candles_in_cache(
                redis_client=redis_client,
                key=key,
                candles=combined,
                limit=limit,
                logger=logger,
            )
        except HTTPException:
            raise
        except Exception:
            if logger:
                logger.warning(
                    "Failed to update Redis cache for %s interval=%s",
                    symbol,
                    interval,
                    exc_info=True,
                )

        return combined

    async def _fetch_cached_candles(
        *,
        redis_client: Any,
        key: str,
        limit: int | None,
        logger: Any,
    ) -> list[dict[str, Any]]:
        try:
            if limit and limit > 0:
                raw_entries = await redis_client.lrange(key, -int(limit), -1)
            else:
                raw_entries = await redis_client.lrange(key, 0, -1)
        except Exception as exc:
            logger.exception(
                "Failed to query chart data from Redis for %s",
                key,
                exc_info=True,
            )
            raise HTTPException(status_code=503, detail="Market data cache unavailable") from exc

        candles: list[dict[str, Any]] = []
        for entry in raw_entries or []:
            if not entry:
                continue
            try:
                decoded = json.loads(entry)
            except (TypeError, ValueError):
                logger.warning("Skipping invalid candle payload: %s", entry)
                continue
            if isinstance(decoded, Mapping):
                candles.append(dict(decoded))
        return candles

    async def _load_candles_with_fallback(
        *,
        query: ChartDataQuery,
        interval: str,
        limit: int | None,
        desired_rows: int,
        duration_minutes: int,
        redis_client: Any,
        logger: Any,
    ) -> tuple[list[dict[str, Any]], CandleCoverage]:
        key = _series_key(query.symbol, interval)
        candles = await _fetch_cached_candles(
            redis_client=redis_client,
            key=key,
            limit=limit,
            logger=logger,
        )

        desired = limit or desired_rows or MAX_RETURNED_CANDLES
        if desired <= 0:
            desired = MAX_RETURNED_CANDLES
        desired = min(int(desired), _MAX_SOURCE_FETCH)

        expected_rows = desired
        if desired_rows > 0:
            expected_rows = max(expected_rows, int(desired_rows))

        fallback_limit = max(desired, MAX_RETURNED_CANDLES)
        fallback_limit = min(fallback_limit, _MAX_SOURCE_FETCH)
        fallback_candles = await _load_candles_from_storage(
            query,
            interval=interval,
            limit=fallback_limit,
            logger=logger,
        )
        if fallback_candles:
            await _store_candles_in_cache(
                redis_client=redis_client,
                key=key,
                candles=fallback_candles,
                limit=fallback_limit,
                logger=logger,
            )
            candles = _combine_candle_sets(candles, fallback_candles)

        coverage, normalised = _calculate_candle_coverage(
            candles,
            expected_rows=expected_rows,
            duration_minutes=duration_minutes,
        )

        if logger:
            logger.debug(
                "Cache coverage for %s interval=%s earliest=%s latest=%s missing_rows=%s missing_minutes=%s",
                query.symbol,
                interval,
                coverage.earliest_ns,
                coverage.latest_ns,
                coverage.missing_rows,
                coverage.missing_minutes,
            )

        if coverage.has_gap and IB_FALLBACK_ENABLED:
            if logger:
                logger.info(
                    "Detected cache gap for %s interval=%s (missing_rows=%s missing_minutes=%s)",
                    query.symbol,
                    interval,
                    coverage.missing_rows,
                    coverage.missing_minutes,
                )

            async def _persist_ib_candles(candles: Sequence[Mapping[str, Any]]) -> None:
                if not candles:
                    return
                cache_limit = max(desired, expected_rows, MAX_RETURNED_CANDLES)
                cache_limit = min(cache_limit, _MAX_SOURCE_FETCH)
                await _persist_candles_for_interval(
                    symbol=query.symbol,
                    interval=interval,
                    candles=candles,
                    redis_client=redis_client,
                    logger=logger,
                    cache_limit=cache_limit,
                )

            missing_segments = _identify_missing_segments(
                normalised,
                interval=interval,
                duration_minutes=duration_minutes,
                expected_rows=expected_rows,
                coverage=coverage,
            )

            ib_candles = await _fetch_candles_from_ib(
                query,
                interval=interval,
                persist=_persist_ib_candles,
                logger=logger,
                missing_segments=missing_segments,
            )

            if ib_candles:
                combined = _combine_candle_sets(normalised, ib_candles)
                coverage, normalised = _calculate_candle_coverage(
                    combined,
                    expected_rows=expected_rows,
                    duration_minutes=duration_minutes,
                )
            elif logger:
                logger.info(
                    "IB fallback returned no additional candles for %s interval=%s",
                    query.symbol,
                    interval,
                )

        trimmed = list(normalised)
        if trimmed and desired > 0 and len(trimmed) > desired:
            trimmed = trimmed[-desired:]

        final_coverage, final_candles = _calculate_candle_coverage(
            trimmed,
            expected_rows=expected_rows,
            duration_minutes=duration_minutes,
        )

        if logger:
            logger.debug(
                "Final coverage for %s interval=%s earliest=%s latest=%s missing_rows=%s missing_minutes=%s",
                query.symbol,
                interval,
                final_coverage.earliest_ns,
                final_coverage.latest_ns,
                final_coverage.missing_rows,
                final_coverage.missing_minutes,
            )

        return final_candles, final_coverage

    def _series_key(symbol: str, interval: str) -> str:
        return f"{cache_prefix}:series:market.candle.{interval}:{symbol}"

    def _get_logger() -> Any:
        return runtime._logger or get_logger(runtime.logger_name)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(title="Market Data Service", lifespan=lifespan)
    app.state.market_runtime = runtime

    @app.websocket("/ws/market")
    async def websocket_market(websocket: WebSocket) -> None:
        await runtime._ws_manager.connect(
            websocket,
            {"type": "snapshot", "data": runtime.get_all_snapshots()},
        )
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            await runtime._ws_manager.disconnect(websocket)

    async def _ensure_redis(logger: Any) -> Any:
        redis_client = runtime._redis
        if redis_client is not None:
            return redis_client
        try:
            redis_client = await runtime.redis_factory()
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.exception("Failed to acquire Redis client for chart data", exc_info=True)
            raise HTTPException(status_code=503, detail="Market data cache unavailable") from exc
        runtime._redis = redis_client
        return redis_client

    @app.get("/market/storage")
    async def market_storage(
        symbol: str = Query(..., description="Ticker symbol"),
        include_parquet: bool = Query(True, alias="includeParquet"),
    ) -> JSONResponse:
        resolved_symbol = (symbol or "").strip().upper()
        if not resolved_symbol:
            raise HTTPException(status_code=400, detail="symbol is required")

        logger = _get_logger()
        redis_client = await _ensure_redis(logger)
        writer = _ensure_parquet_writer(logger) if include_parquet else None

        intervals_payload: dict[str, Any] = {}
        for interval_name in sorted(
            _INTERVAL_TO_SECONDS.keys(), key=lambda key: _INTERVAL_TO_SECONDS[key]
        ):
            key = _series_key(resolved_symbol, interval_name)
            redis_summary = await _summarise_redis_series(redis_client, key)
            interval_payload: dict[str, Any] = {
                "redis": redis_summary,
                "bar_sizes": _INTERVAL_TO_BAR_SIZES.get(interval_name, []),
            }
            if include_parquet:
                interval_payload["parquet"] = _list_parquet_segments(
                    writer,
                    symbol=resolved_symbol,
                    interval=interval_name,
                )
            intervals_payload[interval_name] = interval_payload

        response_payload = {
            "success": True,
            "symbol": resolved_symbol,
            "intervals": intervals_payload,
        }
        return JSONResponse(response_payload)

    @app.post("/market/backfill")
    async def market_backfill(request: BackfillRequest) -> JSONResponse:
        logger = _get_logger()
        if not IB_FALLBACK_ENABLED:
            raise HTTPException(status_code=503, detail="IB fallback disabled")

        bar_key = request.bar_size.strip().lower()
        interval = _BAR_SIZE_TO_INTERVAL.get(bar_key)
        if not interval:
            raise HTTPException(status_code=400, detail="Unsupported bar_size")

        query = ChartDataQuery(
            symbol=request.symbol,
            duration=request.duration,
            bar_size=request.bar_size,
            exchange=request.exchange,
        )

        redis_client = await _ensure_redis(logger)

        extra_inputs: list[str] = []
        if request.extra_bar_sizes:
            extra_inputs.extend(request.extra_bar_sizes)
        if request.extra_intervals:
            extra_inputs.extend(request.extra_intervals)

        extra_intervals: list[str] = []
        source_seconds = _INTERVAL_TO_SECONDS.get(interval, 0)
        for raw_value in extra_inputs:
            if not isinstance(raw_value, str):
                continue
            candidate = raw_value.strip().lower()
            if not candidate:
                continue
            mapped = _BAR_SIZE_TO_INTERVAL.get(candidate, candidate)
            seconds = _INTERVAL_TO_SECONDS.get(mapped)
            if seconds is None or mapped == interval:
                continue
            if source_seconds <= 0 or seconds % source_seconds != 0:
                continue
            if mapped not in extra_intervals:
                extra_intervals.append(mapped)

        persist_summary: dict[str, int] = {}

        async def _persist_ib_candles(candles: Sequence[Mapping[str, Any]]) -> None:
            if not candles:
                return
            cache_limit = min(
                _MAX_SOURCE_FETCH,
                max(len(candles) * 2, MAX_RETURNED_CANDLES),
            )
            stored_primary = await _persist_candles_for_interval(
                symbol=query.symbol,
                interval=interval,
                candles=candles,
                redis_client=redis_client,
                logger=logger,
                cache_limit=cache_limit,
            )
            persist_summary[interval] = len(stored_primary)
            if extra_intervals and stored_primary:
                for extra_interval in extra_intervals:
                    aggregated = _aggregate_candles(
                        stored_primary,
                        target_interval=extra_interval,
                        source_interval=interval,
                    )
                    if not aggregated:
                        continue
                    stored_extra = await _persist_candles_for_interval(
                        symbol=query.symbol,
                        interval=extra_interval,
                        candles=aggregated,
                        redis_client=redis_client,
                        logger=logger,
                    )
                    persist_summary[extra_interval] = len(stored_extra)

        candles = await _fetch_candles_from_ib(
            query,
            interval=interval,
            persist=_persist_ib_candles,
            logger=logger,
        )

        success = bool(candles)
        response_payload: dict[str, Any] = {
            "success": success,
            "symbol": query.symbol,
            "interval": interval,
            "rows": len(candles),
            "stored": persist_summary,
            "intervals": [interval, *extra_intervals],
        }

        redis_summary = await _summarise_redis_series(
            redis_client, _series_key(query.symbol, interval)
        )
        response_payload["redis"] = {interval: redis_summary}
        for extra_interval in extra_intervals:
            response_payload["redis"][extra_interval] = await _summarise_redis_series(
                redis_client,
                _series_key(query.symbol, extra_interval),
            )

        if success:
            response_payload["message"] = f"Fetched {len(candles)} candles from IBKR"
        else:
            response_payload["message"] = "No historical data returned by IBKR"

        return JSONResponse(response_payload)

    async def _load_series(query: ChartDataQuery, *, allow_empty: bool) -> dict[str, Any]:
        logger = _get_logger()
        bar_key = query.bar_size.strip().lower()
        interval = _BAR_SIZE_TO_INTERVAL.get(bar_key)
        if not interval:
            logger.warning("Unsupported bar size for chart data: %s", query.bar_size)
            raise HTTPException(status_code=400, detail="Unsupported bar_size")

        duration_minutes = _parse_duration_to_minutes(query.duration)

        redis_client = await _ensure_redis(logger)

        target_seconds = _INTERVAL_TO_SECONDS.get(interval)
        if not target_seconds:
            logger.warning("No interval mapping for bar size %s", interval)
            raise HTTPException(status_code=400, detail="Unsupported bar_size")

        desired_rows = _estimate_result_rows(duration_minutes, target_seconds)

        result_candles: list[dict[str, Any]] = []
        candidate_intervals = _candidate_source_intervals(interval)

        for idx, source_interval in enumerate(candidate_intervals):
            source_seconds = _INTERVAL_TO_SECONDS.get(source_interval)
            if not source_seconds:
                continue
            source_limit = _estimate_source_limit(
                duration_minutes=duration_minutes,
                target_seconds=target_seconds,
                source_seconds=source_seconds,
                target_rows=desired_rows,
            )
            source_candles, coverage = await _load_candles_with_fallback(
                query=query,
                interval=source_interval,
                limit=source_limit,
                desired_rows=desired_rows,
                duration_minutes=duration_minutes,
                redis_client=redis_client,
                logger=logger,
            )
            if not source_candles:
                continue

            if coverage.has_gap and logger:
                logger.info(
                    "Source interval %s coverage gap remains for %s (missing_rows=%s missing_minutes=%s)",
                    source_interval,
                    query.symbol,
                    coverage.missing_rows,
                    coverage.missing_minutes,
                )

            if source_interval != interval:
                aggregated = _aggregate_candles(
                    source_candles,
                    target_interval=interval,
                    source_interval=source_interval,
                )
            else:
                aggregated = _normalise_candles(source_candles)

            if not aggregated:
                continue

            result_candles = _combine_candle_sets(result_candles, aggregated)
            result_candles = _filter_candles_by_duration(result_candles, duration_minutes)
            if len(result_candles) >= desired_rows:
                break

        if not result_candles and IB_FALLBACK_ENABLED:
            if logger:
                logger.info(
                    "Attempting IB fallback for %s interval=%s duration=%s",
                    query.symbol,
                    interval,
                    query.duration,
                )
            async def _persist_ib_candles(candles: Sequence[Mapping[str, Any]]) -> None:
                cache_limit = min(_MAX_SOURCE_FETCH, max(len(candles) * 2, MAX_RETURNED_CANDLES))
                await _persist_candles_for_interval(
                    symbol=query.symbol,
                    interval=interval,
                    candles=candles,
                    redis_client=redis_client,
                    logger=logger,
                    cache_limit=cache_limit,
                )

            ib_candles = await _fetch_candles_from_ib(
                query,
                interval=interval,
                persist=_persist_ib_candles,
                logger=logger,
            )
            if ib_candles:
                result_candles = _combine_candle_sets(result_candles, ib_candles)
                result_candles = _filter_candles_by_duration(result_candles, duration_minutes)
            elif logger:
                logger.warning(
                    "IB fallback returned no candles for %s interval=%s",
                    query.symbol,
                    interval,
                )

        result_candles = _normalise_candles(result_candles)
        if desired_rows > 0 and len(result_candles) > desired_rows:
            result_candles = result_candles[-desired_rows:]
        if len(result_candles) > MAX_RETURNED_CANDLES:
            result_candles = result_candles[-MAX_RETURNED_CANDLES:]

        if not result_candles:
            if allow_empty:
                return {
                    "candles": [],
                    "interval": interval,
                    "duration_minutes": duration_minutes,
                    "symbol": query.symbol,
                    "duration": query.duration,
                    "bar_size": query.bar_size,
                    "exchange": query.exchange,
                }
            logger.info(
                "Chart data not found for %s interval=%s duration=%s",
                query.symbol,
                interval,
                duration_minutes,
            )
            raise HTTPException(status_code=404, detail="No chart data available")

        payload = _format_candles_for_response(result_candles)
        if not payload:
            if allow_empty:
                return {
                    "candles": [],
                    "interval": interval,
                    "duration_minutes": duration_minutes,
                    "symbol": query.symbol,
                    "duration": query.duration,
                    "bar_size": query.bar_size,
                    "exchange": query.exchange,
                }
            logger.warning("No valid candles after formatting for %s interval=%s", query.symbol, interval)
            raise HTTPException(status_code=404, detail="No chart data available")

        return {
            "candles": payload,
            "interval": interval,
            "duration_minutes": duration_minutes,
            "symbol": query.symbol,
            "duration": query.duration,
            "bar_size": query.bar_size,
            "exchange": query.exchange,
        }

    @app.get("/market/current_price")
    async def current_price(symbol: str = Query(..., description="Ticker symbol")) -> JSONResponse:
        resolved_symbol = (symbol or "").strip().upper()
        if not resolved_symbol:
            raise HTTPException(status_code=400, detail="symbol is required")

        snapshot = runtime.get_snapshot(resolved_symbol)
        if snapshot is None:
            timestamp = to_local_iso(now_local())
            return JSONResponse(
                {
                    "success": False,
                    "symbol": resolved_symbol,
                    "price": None,
                    "timestamp": timestamp,
                    "source": "market_data_service",
                    "error": "No market data available",
                    "data": None,
                }
            )

        price_component = snapshot.get("price")
        raw_timestamp: Any = snapshot.get("updated_at")
        price_value: Any = None

        if isinstance(price_component, Mapping):
            price_value = price_component.get("last")
            raw_timestamp = price_component.get("timestamp") or raw_timestamp
        elif price_component is not None:
            price_value = price_component

        parsed_timestamp = _parse_timestamp(raw_timestamp) if raw_timestamp else None
        timestamp = to_local_iso(parsed_timestamp) if parsed_timestamp else to_local_iso(now_local())

        numeric_price: float | None
        try:
            numeric_price = float(price_value) if price_value is not None else None
        except (TypeError, ValueError):
            numeric_price = None

        success = numeric_price is not None
        response_payload: dict[str, Any] = {
            "success": success,
            "symbol": resolved_symbol,
            "price": numeric_price,
            "timestamp": timestamp,
            "source": "market_data_service",
            "data": price_component if isinstance(price_component, Mapping) else {"value": price_component},
        }

        if not success:
            response_payload["error"] = "No market data available"

        return JSONResponse(response_payload)

    @app.post("/market/historical")
    async def market_historical(query: ChartDataQuery) -> JSONResponse:
        result = await _load_series(query, allow_empty=True)
        candles = result.get("candles", [])
        success = bool(candles)
        response_payload = {
            "success": success,
            "data": candles,
            "symbol": result.get("symbol", query.symbol),
            "duration": result.get("duration", query.duration),
            "bar_size": result.get("bar_size", query.bar_size),
            "exchange": result.get("exchange", query.exchange),
            "rows": len(candles),
            "source": "market_data_service",
            "metadata": {
                "interval": result.get("interval"),
                "duration_minutes": result.get("duration_minutes"),
                "exchange": result.get("exchange"),
            },
        }
        if not success:
            response_payload["error"] = "No chart data available"
        return JSONResponse(response_payload)

    @app.post("/chart_data")
    async def chart_data(query: ChartDataQuery) -> JSONResponse:
        result = await _load_series(query, allow_empty=False)
        response_payload = {
            "success": True,
            "data": result["candles"],
            "symbol": result["symbol"],
            "duration": result["duration"],
            "bar_size": result["bar_size"],
            "exchange": result.get("exchange", query.exchange),
            "source": "market_data_service",
        }
        return JSONResponse(response_payload)

    @app.get("/chart_data")
    async def chart_data_get(
        request: Request,
        symbol: str | None = Query(None, description="Ticker symbol"),
        duration: str | None = Query(None, description="Duration string (e.g. 1 D)"),
        bar_size: str | None = Query(None, alias="barSize"),
        exchange: str | None = Query(None),
    ) -> JSONResponse:
        resolved_symbol = symbol
        resolved_duration = duration
        resolved_bar_size = bar_size
        resolved_exchange = exchange

        if resolved_symbol is None or resolved_duration is None:
            try:
                body = await request.json()
            except Exception:
                body = None
            if isinstance(body, Mapping):
                resolved_symbol = resolved_symbol or body.get("symbol")
                resolved_duration = resolved_duration or body.get("duration")
                resolved_bar_size = (
                    resolved_bar_size
                    or body.get("bar_size")
                    or body.get("barSize")
                )
                resolved_exchange = resolved_exchange or body.get("exchange")

        resolved_bar_size = resolved_bar_size or "1 min"
        resolved_exchange = resolved_exchange or "SMART"

        if not resolved_symbol or not resolved_duration:
            raise HTTPException(
                status_code=400,
                detail="symbol and duration are required as query parameters or JSON body",
            )

        try:
            query = ChartDataQuery(
                symbol=resolved_symbol,
                duration=resolved_duration,
                bar_size=resolved_bar_size,
                exchange=resolved_exchange,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return await chart_data(query)

    @app.get("/market/{symbol}")
    async def get_market(symbol: str) -> JSONResponse:
        snapshot = runtime.get_snapshot(symbol)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="Symbol not found")
        return JSONResponse(snapshot)

    @app.post("/dom/start")
    async def dom_start(payload: DOMStartRequest) -> Dict[str, Any]:
        symbol = (payload.symbol or "").strip().upper()
        if not symbol:
            raise HTTPException(status_code=400, detail="Symbol is required")

        force_restart = bool(payload.force_restart)

        if dom_service.is_stream_active(symbol) and not force_restart:
            return {"success": True, "pending": False, "already_running": True}

        if dom_service.is_start_in_progress(symbol) and not force_restart:
            return {"success": True, "pending": True, "starting": True}

        try:
            start_task = dom_service.request_stream_start(
                symbol,
                num_rows=payload.num_rows,
                force_restart=force_restart,
            )
        except RuntimeError:
            ok = await dom_service.start_stream(
                symbol,
                num_rows=payload.num_rows,
                force_restart=force_restart,
            )
            if not ok:
                error_message, status_code = dom_service.get_last_start_error()
                detail = error_message or "Failed to start depth stream"
                status = (
                    status_code
                    if isinstance(status_code, int) and 400 <= status_code < 600
                    else 500
                )
                raise HTTPException(status_code=status, detail=detail)
            return {"success": True, "pending": False}

        try:
            ok = await asyncio.wait_for(asyncio.shield(start_task), timeout=1.2)
        except asyncio.TimeoutError:
            return {"success": True, "pending": True, "starting": True}
        except Exception as exc:  # pragma: no cover - defensive guard
            DOM_LOGGER.error(
                "DOM start task failed",
                symbol=symbol,
                error=str(exc),
            )
            raise HTTPException(status_code=500, detail="Failed to start depth stream") from exc

        if not ok:
            error_message, status_code = dom_service.get_last_start_error()
            detail = error_message or "Failed to start depth stream"
            status = (
                status_code
                if isinstance(status_code, int) and 400 <= status_code < 600
                else 500
            )
            raise HTTPException(status_code=status, detail=detail)

        return {"success": True, "pending": False, "already_running": False}

    @app.post("/dom/stop")
    async def dom_stop(payload: DOMStopRequest) -> Dict[str, Any]:
        symbol = (payload.symbol or "").strip().upper()
        if not symbol:
            raise HTTPException(status_code=400, detail="Symbol is required")

        stop_task = asyncio.create_task(dom_service.stop_stream(symbol))
        try:
            ok = await asyncio.wait_for(asyncio.shield(stop_task), timeout=DOM_STOP_TIMEOUT)
        except asyncio.TimeoutError:
            DOM_LOGGER.warning(
                "DOM stop request timed out; continuing in background",
                symbol=symbol,
            )

            def _log_result(fut: asyncio.Future) -> None:
                try:
                    result = fut.result()
                except Exception as exc:  # pragma: no cover - defensive guard
                    DOM_LOGGER.warning(
                        "Background DOM stop task failed",
                        symbol=symbol,
                        error=str(exc),
                    )
                else:
                    if result:
                        DOM_LOGGER.info(
                            "DOM stop completed asynchronously",
                            symbol=symbol,
                        )
                    else:
                        DOM_LOGGER.warning(
                            "DOM stop background task reported no active stream",
                            symbol=symbol,
                        )

            stop_task.add_done_callback(_log_result)
            return {"success": True, "pending": True}
        except Exception as exc:
            stop_task.cancel()
            DOM_LOGGER.error(
                "DOM stop task failed",
                symbol=symbol,
                error=str(exc),
            )
            raise HTTPException(status_code=500, detail="Failed to stop depth stream") from exc

        if not ok:
            raise HTTPException(status_code=400, detail="No active depth stream for symbol")

        return {"success": True, "pending": False}

    @app.get("/dom/config")
    async def dom_get_config() -> Dict[str, Any]:
        return {"success": True, "config": dom_service.get_config()}

    @app.put("/dom/config")
    async def dom_update_config(config: DOMConfigModel) -> Dict[str, Any]:
        dom_service.update_config(config.window, config.max_levels, config.trend_thresholds)
        return {"success": True, "config": dom_service.get_config()}

    @app.get("/dom/data")
    async def dom_get_data(symbol: Optional[str] = Query(None)) -> Dict[str, Any]:
        return {
            "success": True,
            "metrics": dom_service.get_metrics(symbol),
            "depth": dom_service.get_last_depth(symbol),
        }

    @app.get("/dom/history/{symbol}")
    async def dom_history(
        symbol: str,
        limit: int = Query(500, ge=0),
        start: Optional[str] = None,
        end: Optional[str] = None,
        include_snapshots: bool = Query(True),
    ) -> Dict[str, Any]:
        history = dom_service.get_history(
            symbol,
            limit=None if limit == 0 else limit,
            start_time=start,
            end_time=end,
            include_snapshots=include_snapshots,
        )
        return {"success": True, "symbol": symbol, **history}

    @app.get("/dom/stats/{symbol}")
    async def dom_stats(
        symbol: str,
        lookback_seconds: Optional[int] = Query(None, ge=60),
        start: Optional[str] = None,
        end: Optional[str] = None,
        max_points: Optional[int] = Query(None, ge=10, le=5000),
    ) -> Dict[str, Any]:
        response = await dom_service.get_stats_async(
            symbol,
            lookback_seconds=lookback_seconds,
            start_time=start,
            end_time=end,
            max_points=max_points,
        )
        response["success"] = True
        return response

    @app.get("/dom/alerts")
    async def dom_alerts() -> Dict[str, Any]:
        return {"success": True, "alerts": dom_service.list_alerts()}

    @app.post("/dom/alerts/{alert_id}/ack")
    async def dom_alert_ack(alert_id: int) -> Dict[str, Any]:
        ok = dom_service.ack_alert(alert_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Alert not found")
        return {"success": True}

    @app.websocket("/ws/dom/{symbol}")
    async def dom_ws(websocket: WebSocket, symbol: str) -> None:
        await websocket.accept()

        loop = asyncio.get_running_loop()
        symbol_arg = symbol
        symbol_key = (symbol_arg or "").upper()
        update_queue: "asyncio.Queue[tuple[Optional[Dict[str, Any]], Any]]" = asyncio.Queue(maxsize=1)

        def _resolve_timestamp(metrics: Optional[Dict[str, Any]]) -> int:
            now_ms = int(time.time() * 1000)
            if not isinstance(metrics, dict):
                return now_ms

            def _coerce(value: Any) -> Optional[int]:
                if isinstance(value, (int, float)):
                    if value > 1e15:
                        return int(value / 1_000_000)
                    if value > 1e12:
                        return int(value / 1_000)
                    if value > 1e10:
                        return int(value)
                    return int(value * 1000)
                if isinstance(value, str):
                    text = value.strip()
                    if not text:
                        return None
                    try:
                        numeric = float(text)
                    except ValueError:
                        try:
                            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                        except ValueError:
                            return None
                        else:
                            return int(parsed.timestamp() * 1000)
                    else:
                        return _coerce(numeric)
                return None

            for key in ("timestamp_ms", "timestamp", "timestamp_ns", "ts_ns", "time"):
                if key not in metrics:
                    continue
                value = _coerce(metrics.get(key))
                if value is not None:
                    return value

            return now_ms

        async def _send_update(
            *, metrics: Optional[Dict[str, Any]], events: Any, error: Optional[str] = None
        ) -> None:
            timestamp_ms = _resolve_timestamp(metrics)
            payload_body: Dict[str, Any] = {"metrics": metrics, "events": events}
            if error is not None:
                payload_body["error"] = error

            payload_data = payload_body.copy()
            payload_data["timestamp"] = timestamp_ms
            payload_data["timestamp_ms"] = timestamp_ms
            message: Dict[str, Any] = {
                "type": "dom_update",
                "symbol": symbol,
                "timestamp": timestamp_ms,
                "timestamp_ms": timestamp_ms,
                **payload_body,
            }
            message["payload"] = payload_data

            await websocket.send_json(message)

        def _make_signature(value: Any) -> Optional[str]:
            if value is None:
                return None
            if isinstance(value, dict) and not value:
                return None
            try:
                return json.dumps(value, sort_keys=True, default=str)
            except (TypeError, ValueError):
                return repr(value)

        def _get_events() -> Any:
            provider = dom_service.get_events
            try:
                return provider(symbol_arg)
            except TypeError:
                return provider()

        async def _enqueue_update(metrics: Optional[Dict[str, Any]], events: Any) -> None:
            while True:
                try:
                    update_queue.get_nowait()
                    update_queue.task_done()
                except asyncio.QueueEmpty:
                    break
            await update_queue.put((metrics, events))

        def _listener(event_symbol: str, metrics: Dict[str, Any], _snapshot: Dict[str, Any]) -> None:
            if symbol_key and event_symbol.upper() != symbol_key:
                return

            metrics_copy = dict(metrics) if metrics else None
            events = _get_events()

            try:
                asyncio.run_coroutine_threadsafe(
                    _enqueue_update(metrics_copy, events),
                    loop,
                )
            except RuntimeError:
                pass

        listener_registered = False

        try:
            dom_service.add_listener(_listener)
            listener_registered = True

            last_metrics_signature: Optional[str] = None
            last_events_signature: Optional[str] = None
            last_error: Optional[str] = None

            initial_metrics = dom_service.get_metrics(symbol_arg)
            initial_events = _get_events()
            await _enqueue_update(initial_metrics, initial_events)

            while True:
                try:
                    metrics, events = await asyncio.wait_for(update_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    metrics = dom_service.get_metrics(symbol_arg)
                    events = _get_events()
                else:
                    update_queue.task_done()

                metrics_signature = _make_signature(metrics)
                events_signature = _make_signature(events)

                if metrics_signature and metrics_signature != last_metrics_signature:
                    await _send_update(metrics=metrics, events=events)
                    last_metrics_signature = metrics_signature
                    last_events_signature = events_signature
                    last_error = None
                    continue

                if events_signature != last_events_signature:
                    await _send_update(
                        metrics=metrics if metrics_signature else None,
                        events=events,
                    )
                    last_events_signature = events_signature
                    if not metrics_signature:
                        last_metrics_signature = None
                    last_error = None
                    continue

                if not metrics_signature:
                    error_message = "DOM metrics unavailable"
                else:
                    error_message = "DOM metrics stale: no new data received"

                if last_error != error_message:
                    await _send_update(metrics=None, events=events, error=error_message)
                    last_error = error_message
                    last_events_signature = events_signature
                    if not metrics_signature:
                        last_metrics_signature = None
        except WebSocketDisconnect:
            return
        finally:
            if listener_registered:
                dom_service.remove_listener(_listener)

    return app


app = create_app()
