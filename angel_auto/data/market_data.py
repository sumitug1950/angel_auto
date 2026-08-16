"""Tick cache, candle aggregator, and live option-chain snapshot - the market data layer
the strategy reads from. No broker calls here - ticks arrive via the broker WebSocket
callback (broker/angelone_ws.py) and get fed in through add_tick()/update_ltp().
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd

from angel_auto.analytics.black_scholes import bs_greeks
from angel_auto.analytics.iv_solver import IVSolverError, solve_iv
from angel_auto.logging_conf import get_logger

log = get_logger(__name__)


@dataclass
class Candle:
    start: datetime
    open: float
    high: float
    low: float
    close: float


class BarAggregator:
    """Buckets tick LTPs into fixed-width candles (candle_interval_sec, e.g. 15s for MACD)."""

    def __init__(self, interval_sec: int, max_candles: int = 1000) -> None:
        self.interval_sec = interval_sec
        self.max_candles = max_candles
        self._candles: list[Candle] = []

    def add_tick(self, price: float, timestamp: datetime | None = None) -> None:
        timestamp = timestamp or datetime.now(timezone.utc)
        bucket_start = self._bucket_start(timestamp)
        if self._candles and self._candles[-1].start == bucket_start:
            candle = self._candles[-1]
            candle.high = max(candle.high, price)
            candle.low = min(candle.low, price)
            candle.close = price
        else:
            self._candles.append(Candle(start=bucket_start, open=price, high=price, low=price, close=price))
            if len(self._candles) > self.max_candles:
                self._candles.pop(0)

    def _bucket_start(self, timestamp: datetime) -> datetime:
        epoch_sec = timestamp.timestamp()
        bucket_epoch = math.floor(epoch_sec / self.interval_sec) * self.interval_sec
        return datetime.fromtimestamp(bucket_epoch, tz=timezone.utc)

    def closes_series(self) -> pd.Series:
        if not self._candles:
            return pd.Series(dtype=float)
        return pd.Series([c.close for c in self._candles], index=[c.start for c in self._candles])

    @property
    def candle_count(self) -> int:
        return len(self._candles)


@dataclass
class OptionQuote:
    token: str
    trading_symbol: str
    strike: float
    option_type: str  # "CE" | "PE"
    expiry: str = ""  # "DDMMMYYYY" - required for quotes_for_type_and_expiry filtering
    ltp: float = 0.0
    iv: float | None = None
    delta: float | None = None


class OptionChainSnapshot:
    """Live per-strike LTP/IV/delta. `register()` once per token (from the instrument
    master), then `update_ltp()` on every tick and `refresh_greeks()` periodically.

    Can hold quotes for more than one expiry at once (the backtest engine registers both
    the DEBIT-relevant monthly and CREDIT-relevant nearest expiry up front, since it
    doesn't know which the strategy will pick until it decides). Strike selection MUST
    filter by expiry, not just option_type - two different expiries can share the same
    strike, and without filtering, a position's ITM and OTM legs could silently end up on
    two different contracts entirely (found via a real backtest run producing exactly
    that: an ITM leg on the monthly expiry and an OTM leg on the nearest expiry).
    """

    def __init__(self) -> None:
        self._quotes: dict[str, OptionQuote] = {}

    def register(self, token: str, trading_symbol: str, strike: float, option_type: str, expiry: str = "") -> None:
        self._quotes.setdefault(
            token,
            OptionQuote(token=token, trading_symbol=trading_symbol, strike=strike, option_type=option_type, expiry=expiry),
        )

    def update_ltp(self, token: str, ltp: float) -> None:
        quote = self._quotes.get(token)
        if quote is not None:
            quote.ltp = ltp

    def get(self, token: str) -> OptionQuote | None:
        return self._quotes.get(token)

    def all_quotes(self) -> list[OptionQuote]:
        return list(self._quotes.values())

    def quotes_for_type(self, option_type: str) -> list[OptionQuote]:
        return [q for q in self._quotes.values() if q.option_type == option_type]

    def quotes_for_type_and_expiry(self, option_type: str, expiry: str) -> list[OptionQuote]:
        return [q for q in self._quotes.values() if q.option_type == option_type and q.expiry == expiry]


def refresh_option_chain_greeks(
    snapshot: OptionChainSnapshot,
    spot: float,
    time_to_expiry_years: float,
    rate: float,
) -> None:
    """Recompute each quote's own IV (from its live LTP, via iv_solver) and delta (from that
    per-strike IV, via Black-Scholes) - respects the real volatility skew across strikes
    rather than assuming one flat IV for the whole chain. A quote whose IV can't be solved
    (stale/bad price, e.g. ltp <= 0) is left with iv=None/delta=None and skipped by strike
    selection, which falls back to a simpler method (see strategy module).
    """
    for quote in snapshot.all_quotes():
        if quote.ltp <= 0:
            quote.iv = None
            quote.delta = None
            continue
        try:
            iv = solve_iv(quote.ltp, spot, quote.strike, time_to_expiry_years, rate, quote.option_type)
            greeks = bs_greeks(spot, quote.strike, time_to_expiry_years, rate, iv, quote.option_type)
            quote.iv = iv
            quote.delta = greeks.delta
        except IVSolverError:
            quote.iv = None
            quote.delta = None
