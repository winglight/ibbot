import time
from datetime import datetime, timedelta

from zoneinfo import ZoneInfo

from src.dom_cache import DomCacheManager


def _append_sample(cache: DomCacheManager, symbol: str, timestamp_ms: int, value: int) -> None:
    snapshot = {"timestamp": timestamp_ms, "bids": [], "asks": []}
    metrics = {"timestamp": timestamp_ms, "value": value}
    cache.append(symbol, snapshot, metrics)


def test_get_history_supports_limit_and_totals(tmp_path) -> None:
    cache = DomCacheManager(
        base_dir=str(tmp_path), retention_seconds=60 * 60, save_interval_seconds=1
    )
    tz = ZoneInfo("Asia/Shanghai")
    base_dt = datetime.fromtimestamp(time.time(), tz)
    base_dt = base_dt.replace(microsecond=0)
    base_ms = int(base_dt.timestamp() * 1000)

    symbol = "TEST"
    for idx in range(6):
        ts_ms = base_ms + idx * 60_000
        _append_sample(cache, symbol, ts_ms, idx)

    history = cache.get_history(symbol, limit=3)

    assert history["total_metrics"] == 6
    assert history["total_snapshots"] == 6
    assert [entry["value"] for entry in history["metrics"]] == [3, 4, 5]
    assert len(history["snapshots"]) == 3


def test_get_history_filters_by_range_and_can_skip_snapshots(tmp_path) -> None:
    cache = DomCacheManager(
        base_dir=str(tmp_path), retention_seconds=60 * 60, save_interval_seconds=1
    )
    tz = ZoneInfo("Asia/Shanghai")
    base_dt = datetime.fromtimestamp(time.time(), tz)
    base_dt = base_dt.replace(microsecond=0)
    base_ms = int(base_dt.timestamp() * 1000)

    symbol = "FILTER"
    for idx in range(5):
        ts_ms = base_ms + idx * 30_000
        _append_sample(cache, symbol, ts_ms, idx)

    start_iso = (base_dt + timedelta(seconds=30)).isoformat()
    end_iso = (base_dt + timedelta(seconds=90)).isoformat()

    history = cache.get_history(
        symbol,
        limit=0,
        start_time=start_iso,
        end_time=end_iso,
        include_snapshots=False,
    )

    assert history["total_metrics"] == 3
    assert history["total_snapshots"] == 3
    assert history["snapshots"] == []
    assert [entry["value"] for entry in history["metrics"]] == [1, 2, 3]
