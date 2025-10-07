from __future__ import annotations

import pytest

from src.utils.ibkr_serialization import coerce_ibkr_value


pandas = pytest.importorskip("pandas")


def test_coerce_ibkr_value_converts_dataframe() -> None:
    df = pandas.DataFrame(
        [
            {"open": 1.0, "close": 2.0},
            {"open": 2.0, "close": 3.0},
        ]
    )

    result = coerce_ibkr_value(df)

    assert isinstance(result, list)
    assert result == [
        {"open": 1.0, "close": 2.0},
        {"open": 2.0, "close": 3.0},
    ]


def test_coerce_ibkr_value_converts_series() -> None:
    series = pandas.Series({"a": 1.0, "b": 2.0})

    result = coerce_ibkr_value(series)

    assert isinstance(result, dict)
    assert result == {"a": 1.0, "b": 2.0}
