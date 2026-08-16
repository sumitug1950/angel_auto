"""MACD - the entry-timing signal for the flagship strategy. Computed on the 15-sec Nifty
spot candles built by data/market_data.py's bar aggregator (see settings.strategies.active.
candle_interval_sec).
"""
from __future__ import annotations

from typing import Literal

import pandas as pd

CrossoverState = Literal["BULLISH", "BEARISH", "NONE"]


def compute_macd(closes: pd.Series, fast_period: int = 12, slow_period: int = 26, signal_period: int = 9) -> pd.DataFrame:
    """Standard MACD. `closes` must be time-ordered (oldest first). Returns a DataFrame
    indexed the same as `closes` with columns: macd, signal, histogram.

    Uses pandas' adjust=False EMA (the standard recursive EMA definition) so early values
    are less reliable until at least `slow_period + signal_period` candles have accumulated -
    callers should not act on crossovers before that warm-up point.
    """
    ema_fast = closes.ewm(span=fast_period, adjust=False).mean()
    ema_slow = closes.ewm(span=slow_period, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal_period, adjust=False).mean()
    histogram = macd_line - signal_line

    return pd.DataFrame({"macd": macd_line, "signal": signal_line, "histogram": histogram})


def current_state(macd_df: pd.DataFrame) -> Literal["BULLISH", "BEARISH"]:
    """MACD line above signal line = bullish state, below = bearish. Used to decide whether
    a manual direction request executes immediately or goes Pending (see strategy module)."""
    if macd_df.empty:
        raise ValueError("macd_df is empty - not enough candles yet")
    last = macd_df.iloc[-1]
    return "BULLISH" if last["macd"] > last["signal"] else "BEARISH"


def detect_crossover(macd_df: pd.DataFrame) -> CrossoverState:
    """Did an actual crossover happen on the most recent candle (comparing the last two rows)?
    This is what resolves a Pending direction request and what fires the opposite-crossover exit.
    """
    if len(macd_df) < 2:
        return "NONE"
    prev, curr = macd_df.iloc[-2], macd_df.iloc[-1]
    was_below = prev["macd"] <= prev["signal"]
    now_above = curr["macd"] > curr["signal"]
    if was_below and now_above:
        return "BULLISH"
    was_above = prev["macd"] >= prev["signal"]
    now_below = curr["macd"] < curr["signal"]
    if was_above and now_below:
        return "BEARISH"
    return "NONE"
