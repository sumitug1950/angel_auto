import numpy as np
import pandas as pd
import pytest

from angel_auto.analytics.indicators import compute_macd, current_state, detect_crossover


def _closes_from_prices(prices: list[float]) -> pd.Series:
    return pd.Series(prices, index=pd.date_range("2026-08-17 09:15", periods=len(prices), freq="15s"))


def test_macd_columns_and_length():
    closes = _closes_from_prices(list(np.linspace(24800, 24900, 60)))
    macd_df = compute_macd(closes, fast_period=12, slow_period=26, signal_period=9)
    assert list(macd_df.columns) == ["macd", "signal", "histogram"]
    assert len(macd_df) == len(closes)


def test_uptrend_settles_into_bullish_state():
    # sustained uptrend should end with MACD line above signal line
    closes = _closes_from_prices(list(np.linspace(24800, 25200, 100)))
    macd_df = compute_macd(closes)
    assert current_state(macd_df) == "BULLISH"


def test_downtrend_settles_into_bearish_state():
    closes = _closes_from_prices(list(np.linspace(25200, 24800, 100)))
    macd_df = compute_macd(closes)
    assert current_state(macd_df) == "BEARISH"


def test_detect_crossover_on_synthetic_flip():
    # flat-bearish-ish segment, then a sharp rally forcing a bullish crossover partway through
    down = list(np.linspace(25000, 24800, 40))
    up = list(np.linspace(24800, 25400, 40))
    closes = _closes_from_prices(down + up)
    macd_df = compute_macd(closes)

    crossovers = [detect_crossover(macd_df.iloc[: i + 1]) for i in range(1, len(macd_df))]
    assert "BULLISH" in crossovers


def test_no_crossover_with_fewer_than_two_candles():
    closes = _closes_from_prices([24800.0])
    macd_df = compute_macd(closes)
    assert detect_crossover(macd_df) == "NONE"


def test_current_state_raises_on_empty():
    empty_df = compute_macd(_closes_from_prices([]))
    with pytest.raises(ValueError):
        current_state(empty_df)
