"""Historical data loader for backtesting - daily NIFTY spot + India VIX closes over a
date range.

Daily granularity, not the live system's 15-sec candles: Angel One's intraday historical
endpoint (ONE_MINUTE/FIVE_MINUTE/FIFTEEN_MINUTE) returned zero candles even for recent
trading days when this was tested (2026-08-16) - daily data was solid going back several
months. The backtest is therefore an approximation of live timing, not a byte-for-byte
replay - useful for validating strategy mechanics (structure selection, strike/expiry
choice, exit behavior, IV Rank distribution) rather than exact MACD crossover timing.
See backtest/engine.py for how this gets used.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from angel_auto.broker.angelone_auth import AngelSession
from angel_auto.data.historical import fetch_daily_candles
from angel_auto.logging_conf import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class DailyBar:
    trade_date: date
    spot_close: float
    vix_close: float


def load_backtest_series(
    session: AngelSession, spot_token: str, vix_token: str, from_date: date, to_date: date
) -> list[DailyBar]:
    """Only dates where *both* spot and VIX closes exist are kept - keeps the two series
    aligned day-for-day without needing separate gap-filling logic."""
    spot_candles = {c["date"]: c["close"] for c in fetch_daily_candles(session, "NSE", spot_token, from_date, to_date)}
    vix_candles = {c["date"]: c["close"] for c in fetch_daily_candles(session, "NSE", vix_token, from_date, to_date)}

    bars = [
        DailyBar(trade_date=d, spot_close=spot_candles[d], vix_close=vix_candles[d])
        for d in sorted(spot_candles)
        if d in vix_candles
    ]
    log.info("backtest_series_loaded", bar_count=len(bars), from_date=str(from_date), to_date=str(to_date))
    return bars
