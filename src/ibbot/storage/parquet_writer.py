"""Thread-safe Parquet writer utilities for market data snapshots."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, Mapping, MutableMapping

logger = logging.getLogger(__name__)


def _truthy(value: str) -> bool:
    return value.lower() in {"1", "true", "yes", "on"}


def _falsy(value: str) -> bool:
    return value.lower() in {"0", "false", "no", "off"}


def _probe_pyarrow() -> tuple[bool, str | None]:
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import pyarrow"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
        )
    except Exception as exc:  # pragma: no cover - probe failures are benign
        return False, f"probe failure: {exc}"

    if result.returncode != 0:
        stderr = result.stderr.decode(errors="ignore").strip()
        reason = stderr or f"exit code {result.returncode}"
        return False, reason
    return True, None


_enable_env = (os.getenv("IBBOT_ENABLE_PARQUET") or "auto").strip().lower()
_disable_env = (os.getenv("IBBOT_DISABLE_PARQUET") or "").strip().lower()

if _disable_env and _truthy(_disable_env):
    _pyarrow_target = False
    _pyarrow_reason = "disabled via IBBOT_DISABLE_PARQUET"
else:
    if _enable_env != "auto":
        if _falsy(_enable_env):
            _pyarrow_target = False
            _pyarrow_reason = "disabled via IBBOT_ENABLE_PARQUET"
        else:
            _pyarrow_target = True
            _pyarrow_reason = None
    else:
        _pyarrow_target = True
        _pyarrow_reason = None

if _pyarrow_target:
    _probe_success, _probe_reason = _probe_pyarrow()
    if not _probe_success:
        _pyarrow_target = False
        _pyarrow_reason = _probe_reason or "pyarrow import probe failed"
else:
    _probe_reason = _pyarrow_reason

if _pyarrow_target:
    try:  # pragma: no cover - optional dependency guard
        import pyarrow as pa
        import pyarrow.compute as pc
        import pyarrow.parquet as pq
    except Exception as exc:  # pragma: no cover - optional dependency
        logger.warning("PyArrow import failed, disabling Parquet support: %s", exc)
        pa = None  # type: ignore[assignment]
        pc = None  # type: ignore[assignment]
        pq = None  # type: ignore[assignment]
        _PYARROW_ERROR: Exception | None = exc
        _PYARROW_REASON = str(exc)
        _PARQUET_AVAILABLE = False
    else:  # pragma: no cover - import side effects
        _PYARROW_ERROR = None
        _PYARROW_REASON = None
        _PARQUET_AVAILABLE = True
else:
    pa = None  # type: ignore[assignment]
    pc = None  # type: ignore[assignment]
    pq = None  # type: ignore[assignment]
    reason = _pyarrow_reason or "pyarrow disabled or unavailable"
    _PYARROW_ERROR = RuntimeError(reason)
    _PYARROW_REASON = reason
    _PARQUET_AVAILABLE = False
    logger.debug("PyArrow support disabled: %s", reason)

__all__ = [
    "MarketDataParquetWriter",
    "get_market_data_writer",
]


if _PARQUET_AVAILABLE:  # pragma: no cover - schema definitions are deterministic
    _DOM_SCHEMA = pa.schema(
        [
            pa.field("timestamp", pa.timestamp("ns", tz="UTC")),
            pa.field("timestamp_ns", pa.int64()),
            pa.field("symbol", pa.string()),
            pa.field(
                "bids",
                pa.list_(
                    pa.struct(
                        [
                            pa.field("level", pa.int32()),
                            pa.field("price", pa.float64()),
                            pa.field("size", pa.float64()),
                        ]
                    )
                ),
            ),
            pa.field(
                "asks",
                pa.list_(
                    pa.struct(
                        [
                            pa.field("level", pa.int32()),
                            pa.field("price", pa.float64()),
                            pa.field("size", pa.float64()),
                        ]
                    )
                ),
            ),
        ]
    )

    _CANDLE_SCHEMA = pa.schema(
        [
            pa.field("timestamp", pa.timestamp("ns", tz="UTC")),
            pa.field("timestamp_ns", pa.int64()),
            pa.field("symbol", pa.string()),
            pa.field("interval", pa.string()),
            pa.field("interval_start_ns", pa.int64()),
            pa.field("interval_end_ns", pa.int64()),
            pa.field("open", pa.float64()),
            pa.field("high", pa.float64()),
            pa.field("low", pa.float64()),
            pa.field("close", pa.float64()),
            pa.field("volume", pa.float64()),
            pa.field("count", pa.int64()),
            pa.field("is_closed", pa.bool_()),
        ]
    )
else:  # pragma: no cover - executed in environments without pyarrow
    _DOM_SCHEMA = None
    _CANDLE_SCHEMA = None


def _ensure_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    raise TypeError(f"Unsupported timestamp value: {value!r}")


def _ensure_timestamp_ns(value: Any) -> int:
    if value is None:
        raise TypeError("timestamp value is required")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, datetime):
        ts = value.astimezone(timezone.utc)
        return int(ts.timestamp() * 1_000_000_000)
    raise TypeError(f"Cannot coerce {value!r} to timestamp_ns")


def _sanitize_component(component: str) -> str:
    return component.replace("/", "_").replace("\\", "_").strip()


def _ensure_pyarrow_available() -> None:
    if pa is None or pq is None or pc is None or _DOM_SCHEMA is None or _CANDLE_SCHEMA is None:
        raise RuntimeError("pyarrow is required for Parquet operations") from _PYARROW_ERROR


if _PARQUET_AVAILABLE:

    @dataclass
    class MarketDataParquetWriter:
        """Persist DOM snapshots and candle aggregates into Parquet files."""

        base_dir: Path | str = Path("data/market_data")
        compression: str = "snappy"
        _locks: MutableMapping[Path, threading.RLock] = field(default_factory=dict, init=False, repr=False)
        _lock_guard: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

        def __post_init__(self) -> None:
            _ensure_pyarrow_available()
            base_path = Path(self.base_dir)
            base_path.mkdir(parents=True, exist_ok=True)
            self.base_dir = base_path
            (self.base_dir / "dom").mkdir(parents=True, exist_ok=True)
            (self.base_dir / "candles").mkdir(parents=True, exist_ok=True)

        # Public API --------------------------------------------------
        def append_dom(self, symbol: str, snapshot: Mapping[str, Any]) -> None:
            symbol_key = _sanitize_component(symbol.upper())
            timestamp = _ensure_datetime(snapshot.get("timestamp"))
            timestamp_ns = _ensure_timestamp_ns(snapshot.get("timestamp") or timestamp)
            record = {
                "timestamp": timestamp,
                "timestamp_ns": timestamp_ns,
                "symbol": symbol_key,
                "bids": [
                    {
                        "level": int(level.get("level", 0)),
                        "price": float(level.get("price", 0.0) or 0.0),
                        "size": float(level.get("size", 0.0) or 0.0),
                    }
                    for level in snapshot.get("bids", [])
                ],
                "asks": [
                    {
                        "level": int(level.get("level", 0)),
                        "price": float(level.get("price", 0.0) or 0.0),
                        "size": float(level.get("size", 0.0) or 0.0),
                    }
                    for level in snapshot.get("asks", [])
                ],
            }
            table = pa.Table.from_pylist([record], schema=_DOM_SCHEMA)
            path = self._dom_path(symbol_key, timestamp)
            self._append_table(path, table)

        def append_candle(
            self,
            symbol: str,
            interval: str,
            candle: Mapping[str, Any],
            *,
            timestamp_ns: int | None = None,
        ) -> None:
            symbol_key = _sanitize_component(symbol.upper())
            interval_key = _sanitize_component(interval.lower())
            ts_ns = _ensure_timestamp_ns(
                timestamp_ns
                or candle.get("interval_end_ns")
                or candle.get("timestamp_ns")
                or candle.get("timestamp")
            )
            timestamp = datetime.fromtimestamp(ts_ns / 1_000_000_000, tz=timezone.utc)
            record = {
                "timestamp": timestamp,
                "timestamp_ns": ts_ns,
                "symbol": symbol_key,
                "interval": interval_key,
                "interval_start_ns": _ensure_timestamp_ns(candle.get("interval_start_ns") or ts_ns),
                "interval_end_ns": _ensure_timestamp_ns(candle.get("interval_end_ns") or ts_ns),
                "open": float(candle.get("open", 0.0) or 0.0),
                "high": float(candle.get("high", 0.0) or 0.0),
                "low": float(candle.get("low", 0.0) or 0.0),
                "close": float(candle.get("close", 0.0) or 0.0),
                "volume": float(candle.get("volume", 0.0) or 0.0),
                "count": int(candle.get("count", 0) or 0),
                "is_closed": bool(candle.get("is_closed", False)),
            }
            table = pa.Table.from_pylist([record], schema=_CANDLE_SCHEMA)
            path = self._candle_path(symbol_key, interval_key, timestamp)
            self._append_table(path, table)

        def read_candles(
            self,
            symbol: str,
            interval: str,
            *,
            start: datetime | None = None,
            end: datetime | None = None,
            limit: int | None = None,
        ) -> list[dict[str, Any]]:
            symbol_key = _sanitize_component(symbol.upper())
            interval_key = _sanitize_component(interval.lower())
            directory = self.base_dir / "candles" / interval_key / symbol_key
            if not directory.exists():
                return []

            start_ts = _ensure_datetime(start) if start else None
            end_ts = _ensure_datetime(end) if end else None
            start_scalar = (
                pa.scalar(start_ts, type=_CANDLE_SCHEMA.field("timestamp").type) if start_ts else None
            )
            end_scalar = pa.scalar(end_ts, type=_CANDLE_SCHEMA.field("timestamp").type) if end_ts else None

            records: list[dict[str, Any]] = []
            for file_path in sorted(directory.glob("*.parquet")):
                table = pq.read_table(file_path)
                mask = None
                if start_scalar is not None:
                    mask = pc.greater_equal(table.column("timestamp"), start_scalar)
                if end_scalar is not None:
                    end_mask = pc.less_equal(table.column("timestamp"), end_scalar)
                    mask = end_mask if mask is None else pc.and_(mask, end_mask)
                if mask is not None:
                    table = table.filter(mask)
                if table.num_rows == 0:
                    continue
                records.extend(table.to_pylist())

            if not records:
                return []

            records.sort(key=lambda item: item.get("timestamp_ns", 0))
            if limit is not None and limit > 0:
                records = records[-int(limit) :]

            result: list[dict[str, Any]] = []
            for item in records:
                candle_payload = {
                    "symbol": symbol_key,
                    "interval": interval_key,
                    "interval_start_ns": int(item.get("interval_start_ns", 0) or 0),
                    "interval_end_ns": int(item.get("interval_end_ns", 0) or 0),
                    "open": float(item.get("open", 0.0) or 0.0),
                    "high": float(item.get("high", 0.0) or 0.0),
                    "low": float(item.get("low", 0.0) or 0.0),
                    "close": float(item.get("close", 0.0) or 0.0),
                    "volume": float(item.get("volume", 0.0) or 0.0),
                    "count": int(item.get("count", 0) or 0),
                    "is_closed": bool(item.get("is_closed", False)),
                }
                # Preserve ISO timestamp for diagnostics if present.
                timestamp = item.get("timestamp")
                if isinstance(timestamp, datetime):
                    candle_payload["timestamp"] = timestamp.isoformat()
                result.append(candle_payload)
            return result

        def read_dom(
            self,
            symbol: str,
            *,
            start: datetime | None = None,
            end: datetime | None = None,
            limit: int | None = None,
        ) -> list[dict[str, Any]]:
            symbol_key = _sanitize_component(symbol.upper())
            directory = self.base_dir / "dom" / symbol_key
            if not directory.exists():
                return []

            start_ts = _ensure_datetime(start) if start else None
            end_ts = _ensure_datetime(end) if end else None
            start_scalar = (
                pa.scalar(start_ts, type=_DOM_SCHEMA.field("timestamp").type) if start_ts else None
            )
            end_scalar = pa.scalar(end_ts, type=_DOM_SCHEMA.field("timestamp").type) if end_ts else None

            records: list[dict[str, Any]] = []
            for file_path in sorted(directory.glob("*.parquet")):
                table = pq.read_table(file_path)
                mask = None
                if start_scalar is not None:
                    mask = pc.greater_equal(table.column("timestamp"), start_scalar)
                if end_scalar is not None:
                    end_mask = pc.less_equal(table.column("timestamp"), end_scalar)
                    mask = end_mask if mask is None else pc.and_(mask, end_mask)
                if mask is not None:
                    table = table.filter(mask)
                if table.num_rows == 0:
                    continue
                records.extend(table.to_pylist())

            if not records:
                return []

            records.sort(key=lambda item: item.get("timestamp_ns", 0))
            if limit is not None and limit > 0:
                records = records[-int(limit) :]

            snapshots: list[dict[str, Any]] = []
            for entry in records:
                timestamp = entry.get("timestamp")
                if isinstance(timestamp, datetime):
                    ts_iso = timestamp.isoformat()
                else:
                    ts_iso = None
                snapshot = {
                    "symbol": symbol_key,
                    "timestamp": ts_iso,
                    "bids": entry.get("bids", []),
                    "asks": entry.get("asks", []),
                }
                snapshots.append(snapshot)
            return snapshots

        # Internal helpers --------------------------------------------
        def _dom_path(self, symbol: str, timestamp: datetime) -> Path:
            directory = self.base_dir / "dom" / symbol
            directory.mkdir(parents=True, exist_ok=True)
            return directory / f"{timestamp:%Y%m%d}.parquet"

        def _candle_path(self, symbol: str, interval: str, timestamp: datetime) -> Path:
            directory = self.base_dir / "candles" / interval / symbol
            directory.mkdir(parents=True, exist_ok=True)
            return directory / f"{timestamp:%Y%m%d}.parquet"

        def _get_lock(self, path: Path) -> threading.RLock:
            with self._lock_guard:
                lock = self._locks.get(path)
                if lock is None:
                    lock = threading.RLock()
                    self._locks[path] = lock
                return lock

        def _append_table(self, path: Path, table: pa.Table) -> None:
            lock = self._get_lock(path)
            with lock:
                if path.exists():
                    existing = pq.read_table(path)
                    combined = pa.concat_tables([existing, table], promote=True)
                    pq.write_table(combined, path, compression=self.compression)
                else:
                    pq.write_table(table, path, compression=self.compression)

else:

    @dataclass
    class MarketDataParquetWriter:
        """No-op writer used when PyArrow support is unavailable."""

        base_dir: Path | str = Path("data/market_data")
        compression: str = "snappy"
        _reason: str = field(default=_PYARROW_REASON or "pyarrow unavailable", init=False, repr=False)
        _warning_emitted: ClassVar[bool] = False

        def __post_init__(self) -> None:
            base_path = Path(self.base_dir)
            try:
                base_path.mkdir(parents=True, exist_ok=True)
            except Exception:  # pragma: no cover - defensive guard
                pass
            self.base_dir = base_path
            if not MarketDataParquetWriter._warning_emitted:
                logger.warning(
                    "PyArrow unavailable (%s); MarketDataParquetWriter running in no-op mode.",
                    self._reason,
                )
                MarketDataParquetWriter._warning_emitted = True

        # Public API --------------------------------------------------
        def append_dom(self, symbol: str, snapshot: Mapping[str, Any]) -> None:  # pragma: no cover - trivial
            return None

        def append_candle(
            self,
            symbol: str,
            interval: str,
            candle: Mapping[str, Any],
            *,
            timestamp_ns: int | None = None,
        ) -> None:  # pragma: no cover - trivial
            return None

        def read_candles(
            self,
            symbol: str,
            interval: str,
            *,
            start: datetime | None = None,
            end: datetime | None = None,
            limit: int | None = None,
        ) -> list[dict[str, Any]]:  # pragma: no cover - trivial
            return []

        def read_dom(
            self,
            symbol: str,
            *,
            start: datetime | None = None,
            end: datetime | None = None,
            limit: int | None = None,
        ) -> list[dict[str, Any]]:  # pragma: no cover - trivial
            return []


_global_writer: MarketDataParquetWriter | None = None
_global_writer_lock = threading.Lock()


def get_market_data_writer() -> MarketDataParquetWriter:
    """Return a module-wide singleton Parquet writer instance."""

    global _global_writer
    if _global_writer is not None:
        return _global_writer
    with _global_writer_lock:
        if _global_writer is None:
            base_dir = os.getenv("MARKET_DATA_STORAGE_PATH") or "data/market_data"
            _global_writer = MarketDataParquetWriter(base_dir=base_dir)
    return _global_writer
