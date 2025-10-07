"""Helpers for inspecting DOM snapshot storage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional

try:  # Optional dependency used when available for richer metadata.
    import pyarrow as pa  # type: ignore
    from pyarrow import parquet as pq  # type: ignore
except Exception:  # pragma: no cover - pyarrow optional at runtime.
    pa = None  # type: ignore
    pq = None  # type: ignore

__all__ = [
    "DomFileSegment",
    "DomStorageSummary",
    "summarise_dom_directory",
]


@dataclass(slots=True)
class DomFileSegment:
    """Metadata extracted from a single DOM Parquet file."""

    file: str
    path: str
    size: int
    rows: Optional[int] = None
    start_ns: Optional[int] = None
    end_ns: Optional[int] = None
    modified_ns: Optional[int] = None

    def to_dict(self) -> dict[str, object]:
        return {
            "file": self.file,
            "path": self.path,
            "size": self.size,
            "rows": self.rows,
            "start_ns": self.start_ns,
            "end_ns": self.end_ns,
            "modified_ns": self.modified_ns,
        }


@dataclass(slots=True)
class DomStorageSummary:
    """Aggregated information about DOM snapshots stored on disk."""

    symbol: str
    files: List[DomFileSegment]
    total_rows: int
    earliest_ns: Optional[int]
    latest_ns: Optional[int]

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "files": [segment.to_dict() for segment in self.files],
            "total_rows": self.total_rows,
            "earliest_ns": self.earliest_ns,
            "latest_ns": self.latest_ns,
        }


def _to_timestamp_ns(value: object) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return int(value)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp() * 1_000_000_000)
    return None


def _iter_parquet_files(directory: Path) -> Iterable[Path]:
    if not directory.exists():
        return []
    return sorted(directory.glob("*.parquet"))


def summarise_dom_directory(base_dir: Path | str, symbol: str) -> DomStorageSummary:
    """Collect metadata for DOM snapshots stored under *base_dir*.

    Parameters
    ----------
    base_dir:
        Root directory of the market data storage (containing the ``dom`` folder).
    symbol:
        Symbol identifier used to namespace files.
    """

    base_path = Path(base_dir)
    directory = base_path / "dom" / symbol.upper()

    segments: List[DomFileSegment] = []
    earliest: Optional[int] = None
    latest: Optional[int] = None
    total_rows = 0

    if not directory.exists():
        return DomStorageSummary(symbol=symbol.upper(), files=[], total_rows=0, earliest_ns=None, latest_ns=None)

    parquet_available = pq is not None and pa is not None

    for file_path in _iter_parquet_files(directory):
        stats = file_path.stat()
        segment = DomFileSegment(
            file=file_path.name,
            path=str(file_path),
            size=stats.st_size,
            modified_ns=int(stats.st_mtime * 1_000_000_000),
        )

        if parquet_available:
            try:
                table = pq.read_table(file_path, columns=["timestamp_ns"])
            except Exception:  # pragma: no cover - defensive guard around IO
                table = None
            if table is not None:
                rows = table.num_rows
                segment.rows = rows
                if rows > 0:
                    total_rows += rows
                    first_value = table.column("timestamp_ns")[0].as_py()
                    last_value = table.column("timestamp_ns")[rows - 1].as_py()
                    start_ns = _to_timestamp_ns(first_value)
                    end_ns = _to_timestamp_ns(last_value)
                    segment.start_ns = start_ns
                    segment.end_ns = end_ns
                    if start_ns is not None:
                        earliest = start_ns if earliest is None else min(earliest, start_ns)
                    if end_ns is not None:
                        latest = end_ns if latest is None else max(latest, end_ns)
        segments.append(segment)

    return DomStorageSummary(
        symbol=symbol.upper(),
        files=segments,
        total_rows=total_rows,
        earliest_ns=earliest,
        latest_ns=latest,
    )

