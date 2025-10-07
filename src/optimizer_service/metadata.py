"""Provides static metadata for optimizer configuration."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

from src.ibbot.storage import get_market_data_writer, summarise_dom_directory


DEFAULT_BASE_METRICS: List[str] = ["obi5", "ofi", "trend_score", "cvd", "mid_price"]
DEFAULT_FEATURE_METRICS: List[str] = [
    "stacking",
    "pulling",
    "wall",
    "cancel_rate",
    "absorption",
    "sweep",
    "fake_breakout",
    "wall_pull_no_follow",
    "obi_ofi_reversal",
    "spread",
]
DEFAULT_ALGORITHMS: List[str] = ["grid", "random", "bayesian"]


def get_available_base_metrics() -> List[str]:
    return list(DEFAULT_BASE_METRICS)


def get_available_feature_metrics() -> List[str]:
    return list(DEFAULT_FEATURE_METRICS)


def get_available_algorithms() -> List[str]:
    return list(DEFAULT_ALGORITHMS)


def _iso_from_ns(ns: int | None) -> str:
    if ns is None:
        return ""
    try:
        dt = datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc)
    except Exception:
        return ""
    return dt.date().isoformat()


def _iso_from_ns_optional(ns: int | None) -> str | None:
    if ns is None:
        return None
    try:
        dt = datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc)
    except Exception:
        return None
    return dt.isoformat()


def _discover_dom_directories(base_dir: Path) -> Iterable[str]:
    dom_root = base_dir / "dom"
    if not dom_root.exists():
        return []
    return sorted(
        entry.name
        for entry in dom_root.iterdir()
        if entry.is_dir()
    )


def _get_writer() -> Any:
    try:
        return get_market_data_writer()
    except Exception as exc:  # pragma: no cover - handled by callers
        raise RuntimeError(f"Market data storage unavailable: {exc}") from exc


def get_symbol_data_range(symbol: str) -> Dict[str, str]:
    try:
        writer = _get_writer()
    except RuntimeError:
        writer = None

    summary = None
    if writer is not None:
        try:
            summary = summarise_dom_directory(writer.base_dir, symbol)
        except Exception:
            summary = None

    if summary and summary.total_rows:
        start_iso = _iso_from_ns(summary.earliest_ns)
        end_iso = _iso_from_ns(summary.latest_ns) or date.today().isoformat()
        records = summary.total_rows
    else:
        today = date.today()
        start_iso = (today - timedelta(days=60)).isoformat()
        end_iso = today.isoformat()
        records = 0

    return {
        "symbol": symbol,
        "start_date": start_iso,
        "end_date": end_iso,
        "records": records,
    }


def list_dom_symbols() -> List[str]:
    writer = _get_writer()
    base_dir = Path(writer.base_dir)
    return [symbol.upper() for symbol in _discover_dom_directories(base_dir)]


def describe_dom_storage(symbol: str, *, limit: int | None = None) -> Dict[str, Any]:
    writer = _get_writer()
    summary = summarise_dom_directory(writer.base_dir, symbol)

    file_limit = None
    if limit is not None:
        try:
            limit_value = int(limit)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            limit_value = 0
        if limit_value > 0:
            file_limit = limit_value

    segments = summary.files
    if file_limit is not None and len(segments) > file_limit:
        segments = segments[-file_limit:]

    def _segment_payload(segment: Any) -> Dict[str, Any]:
        payload = segment.to_dict()
        payload["start_iso"] = _iso_from_ns_optional(payload.get("start_ns"))
        payload["end_iso"] = _iso_from_ns_optional(payload.get("end_ns"))
        payload["modified_iso"] = _iso_from_ns_optional(payload.get("modified_ns"))
        return payload

    earliest_iso = _iso_from_ns_optional(summary.earliest_ns)
    latest_iso = _iso_from_ns_optional(summary.latest_ns)

    return {
        "symbol": summary.symbol,
        "storage_root": str(Path(writer.base_dir) / "dom" / summary.symbol),
        "file_count": len(summary.files),
        "displayed_files": len(segments),
        "total_rows": summary.total_rows,
        "earliest_ns": summary.earliest_ns,
        "latest_ns": summary.latest_ns,
        "earliest_iso": earliest_iso,
        "latest_iso": latest_iso,
        "files": [_segment_payload(segment) for segment in segments],
    }
