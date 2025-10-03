"""Utility for compressing and rotating on-disk Parquet market data files."""

from __future__ import annotations

import argparse
import gzip
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

DATE_FORMAT = "%Y%m%d"


def _iter_parquet_files(base_dir: Path) -> Iterable[Path]:
    for path in base_dir.rglob("*.parquet"):
        if path.is_file():
            yield path


def _parse_date_from_path(path: Path) -> datetime | None:
    try:
        return datetime.strptime(path.stem, DATE_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _compress_file(path: Path, dry_run: bool) -> Path:
    target = path.with_suffix(path.suffix + ".gz")
    if target.exists():
        return target
    if dry_run:
        return target
    with path.open("rb") as src, gzip.open(target, "wb") as dest:
        shutil.copyfileobj(src, dest)
    return target


def rotate_market_data(
    base_dir: Path,
    *,
    compress_after_days: int | None,
    delete_after_days: int | None,
    dry_run: bool = False,
) -> None:
    now = datetime.now(timezone.utc)
    for file_path in _iter_parquet_files(base_dir):
        file_date = _parse_date_from_path(file_path)
        if file_date is None:
            continue
        age_days = (now - file_date).days

        if compress_after_days is not None and age_days >= compress_after_days:
            target = _compress_file(file_path, dry_run)
            if not dry_run and target.exists():
                file_path.unlink(missing_ok=True)

        if delete_after_days is not None and age_days >= delete_after_days:
            gz_path = file_path.with_suffix(file_path.suffix + ".gz")
            if dry_run:
                continue
            if gz_path.exists():
                gz_path.unlink(missing_ok=True)
            else:
                file_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Rotate/compress market data parquet files")
    parser.add_argument(
        "--base-dir",
        default=os.getenv("MARKET_DATA_STORAGE_PATH", "data/market_data"),
        help="Root directory containing market data parquet files",
    )
    parser.add_argument(
        "--compress-after-days",
        type=int,
        default=7,
        help="Compress parquet files older than the specified number of days",
    )
    parser.add_argument(
        "--delete-after-days",
        type=int,
        default=30,
        help="Delete compressed archives older than the specified number of days",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print actions without modifying files")
    args = parser.parse_args()

    rotate_market_data(
        Path(args.base_dir),
        compress_after_days=args.compress_after_days,
        delete_after_days=args.delete_after_days,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
