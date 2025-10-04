import core
import pandas as pd

from src.database import get_db_manager
from src.routes import market_data as market_data_module


def test_chart_data_fallback_returns_db_cache(monkeypatch, client, admin_headers):
    db_manager = get_db_manager()
    symbol = "TESTFALLBACK"
    bar_size = "1 min"
    interval = db_manager._normalize_interval(bar_size)

    with db_manager.cursor() as cursor:
        cursor.execute(
            "DELETE FROM historical_data WHERE symbol = ? AND interval = ?",
            (symbol, interval),
        )
        cursor.execute(
            """
            INSERT INTO historical_data
                (symbol, date, time, open, high, low, close, volume, interval, adj_close)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                symbol,
                "2024-01-02",
                "10:30:00",
                100.0,
                110.0,
                95.0,
                108.0,
                1234,
                interval,
                108.0,
            ),
        )

    # Pretend we are connected to IB but the API returns no data
    monkeypatch.setattr(core.connection_manager, "is_connected", lambda: True)
    monkeypatch.setattr(core, "ibkr_client", object(), raising=False)

    async def fake_fetch(*args, **kwargs):
        return pd.DataFrame()

    monkeypatch.setattr(
        market_data_module,
        "_fetch_ib_historical",
        fake_fetch,
        raising=False,
    )

    response = client.post(
        "/api/chart_data",
        json={
            "symbol": symbol,
            "duration": "1 D",
            "bar_size": bar_size,
            "exchange": "SMART",
        },
        headers=admin_headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["data"], "Expected fallback data to be returned"
    first = payload["data"][0]
    assert first["close"] == 108.0
    assert first["open"] == 100.0


def test_chart_data_what_to_show_fallback(monkeypatch, client, admin_headers):
    symbol = "FALLBACKMID"
    bar_size = "1 min"

    market_data_module.historical_data_cache.clear()

    db_manager = get_db_manager()
    with db_manager.cursor() as cursor:
        cursor.execute(
            "DELETE FROM historical_data WHERE symbol = ?",
            (symbol,),
        )

    monkeypatch.setattr(core.connection_manager, "is_connected", lambda: True)
    monkeypatch.setattr(core, "ibkr_client", object(), raising=False)

    attempts = []

    async def fake_fetch(*args, **kwargs):
        attempts.append(kwargs.get("what_to_show"))
        if kwargs.get("what_to_show") == "MIDPOINT":
            return pd.DataFrame(
                [
                    {
                        "date": "2024-01-02",
                        "time": "10:00:00",
                        "open": 10.0,
                        "high": 10.5,
                        "low": 9.5,
                        "close": 10.2,
                        "volume": 100,
                    }
                ]
            )
        return pd.DataFrame()

    monkeypatch.setattr(
        market_data_module,
        "_fetch_ib_historical",
        fake_fetch,
        raising=False,
    )

    response = client.post(
        "/api/chart_data",
        json={
            "symbol": symbol,
            "duration": "1 D",
            "bar_size": bar_size,
            "exchange": "SMART",
            "data_type": "TRADES",
            "force_refresh": True,
            "use_cache": False,
        },
        headers=admin_headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["data"], "Expected fallback to return chart data"
    assert payload.get("data_type") == "MIDPOINT"
    assert attempts == ["TRADES", "MIDPOINT"], attempts
