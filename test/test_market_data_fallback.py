import core
import httpx
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

    async def fake_service_post(path, payload):
        request = httpx.Request("POST", f"http://market-data{path}")
        return httpx.Response(status_code=404, request=request)

    monkeypatch.setattr(
        market_data_module,
        "_market_service_post",
        fake_service_post,
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
