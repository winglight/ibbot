"""Tests for selecting the active index future contract."""

from datetime import datetime, timedelta

from zoneinfo import ZoneInfo

import pytest

from src.ibkr_client import (
    IBKRClient,
    NY_TZ,
    _front_quarter_contract,
    _third_friday_datetime,
)


@pytest.mark.parametrize(
    ("current_time", "expected"),
    [
        # Before the March expiry we should still trade the March contract.
        (
            _third_friday_datetime(2024, 3) - timedelta(hours=1),
            (2024, 3),
        ),
        # Shortly after the March expiry the contract should roll to June.
        (
            _third_friday_datetime(2024, 3) + timedelta(hours=1),
            (2024, 6),
        ),
        # In October the active contract must remain the December one
        # instead of skipping forward to the following March.
        (
            datetime(2024, 10, 1, 12, tzinfo=NY_TZ),
            (2024, 12),
        ),
        # Once the December contract expires we advance to the next year's
        # March contract.
        (
            _third_friday_datetime(2024, 12) + timedelta(hours=1),
            (2025, 3),
        ),
    ],
)
def test_front_quarter_contract_rolls_correctly(current_time, expected):
    assert _front_quarter_contract(current_time) == expected


def test_get_future_contract_assigns_quarter(monkeypatch):
    # ``now_local`` returns Asia/Shanghai; convert a NY timestamp to match.
    ny_timestamp = datetime(2024, 10, 1, 12, tzinfo=NY_TZ)
    shanghai_timestamp = ny_timestamp.astimezone(ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr("src.ibkr_client.now_local", lambda: shanghai_timestamp)

    client = IBKRClient()
    contract = client.get_future_contract("MNQ")

    assert contract.lastTradeDateOrContractMonth == "202412"
    assert contract.symbol == "MNQ"
    # Exchange and trading class should follow the pre-defined mapping.
    assert contract.exchange == "CME"
    assert getattr(contract, "tradingClass", None) == "MNQ"
