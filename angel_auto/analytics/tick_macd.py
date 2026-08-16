"""Tick-driven MACD engine for the automatic zero-cross strategies (strategy/
macd_zero_cross_single_leg.py). Unlike analytics.indicators.compute_macd() - which is
candle-based (pandas, operates on BarAggregator's 15-sec closes) - this recomputes the
MACD/Signal EMAs recursively on every single incoming tick, with no candle-aggregation
step at all.

EMA periods here are raw TICK counts, not time/seconds - e.g. fast_period=12 means "the
last 12 ticks", regardless of how much wall-clock time that spans. This was a deliberate,
explicit choice (over a time-seconds-based EMA) for initial testing: it will be much
faster/noisier than the flagship's candle-based MACD since many ticks can arrive within a
single second. Periods are easy to retune later once real behavior has been observed.

Signal definition (confirmed with the user - not the more common "MACD crosses its own
zero line" definition): a normal MACD-line/Signal-line crossover, classified by which side
of the zero line it occurs on:
  - MACD crosses ABOVE Signal while MACD > 0  -> "BULLISH"
  - MACD crosses BELOW Signal while MACD < 0  -> "BEARISH"
  - any other crossover (e.g. MACD crosses above Signal while still negative) -> "NONE",
    it does not count as a signal for these strategies.
"""
from __future__ import annotations

from typing import Literal

CrossSignal = Literal["BULLISH", "BEARISH", "NONE"]


class TickMacdEngine:
    def __init__(self, fast_period: int = 12, slow_period: int = 26, signal_period: int = 9) -> None:
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.signal_period = signal_period
        self._alpha_fast = 2.0 / (fast_period + 1)
        self._alpha_slow = 2.0 / (slow_period + 1)
        self._alpha_signal = 2.0 / (signal_period + 1)

        self._ema_fast: float | None = None
        self._ema_slow: float | None = None
        self._signal_ema: float | None = None
        self._prev_macd: float | None = None
        self._prev_signal: float | None = None

        self.macd: float = 0.0
        self.signal: float = 0.0
        self.histogram: float = 0.0
        self._tick_count = 0

    @property
    def is_warmed_up(self) -> bool:
        """First (slow_period + signal_period) ticks aren't reliable yet - mirrors the
        candle-based compute_macd()'s warm-up caveat."""
        return self._tick_count >= (self.slow_period + self.signal_period)

    @property
    def current_side(self) -> Literal["BULLISH", "BEARISH"]:
        """Which side of the zero line MACD currently sits on - used by the strategy's
        restart-robust exit check (a persistent state check, not a same-tick crossover
        event, so a missed tick never means a missed exit)."""
        return "BULLISH" if self.macd > 0 else "BEARISH"

    def update(self, price: float) -> CrossSignal:
        self._tick_count += 1
        self._ema_fast = price if self._ema_fast is None else self._ema_fast + (price - self._ema_fast) * self._alpha_fast
        self._ema_slow = price if self._ema_slow is None else self._ema_slow + (price - self._ema_slow) * self._alpha_slow
        macd = self._ema_fast - self._ema_slow
        self._signal_ema = macd if self._signal_ema is None else self._signal_ema + (macd - self._signal_ema) * self._alpha_signal
        signal = self._signal_ema

        result: CrossSignal = "NONE"
        if self.is_warmed_up and self._prev_macd is not None and self._prev_signal is not None:
            crossed_up = self._prev_macd <= self._prev_signal and macd > signal
            crossed_down = self._prev_macd >= self._prev_signal and macd < signal
            if crossed_up and macd > 0:
                result = "BULLISH"
            elif crossed_down and macd < 0:
                result = "BEARISH"

        self._prev_macd, self._prev_signal = macd, signal
        self.macd, self.signal, self.histogram = macd, signal, macd - signal
        return result
