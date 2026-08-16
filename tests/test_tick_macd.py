import pytest

from angel_auto.analytics.tick_macd import TickMacdEngine


def _reference_macd_series(prices, fast, slow, signal):
    """Independent, textbook re-implementation of the same recursive EMA formula - used to
    verify the engine's EMA recursion is correct, decoupled from the crossover classifier
    (tested separately below via direct state injection)."""
    alpha_fast, alpha_slow, alpha_signal = 2 / (fast + 1), 2 / (slow + 1), 2 / (signal + 1)
    ema_fast = ema_slow = sig = None
    out = []
    for p in prices:
        ema_fast = p if ema_fast is None else ema_fast + (p - ema_fast) * alpha_fast
        ema_slow = p if ema_slow is None else ema_slow + (p - ema_slow) * alpha_slow
        macd = ema_fast - ema_slow
        sig = macd if sig is None else sig + (macd - sig) * alpha_signal
        out.append((macd, sig))
    return out


def test_engine_matches_reference_ema_recursion():
    prices = [100, 101, 99, 105, 110, 108, 95, 90, 92, 100, 115, 120, 118, 130]
    engine = TickMacdEngine(fast_period=3, slow_period=5, signal_period=3)
    ref = _reference_macd_series(prices, 3, 5, 3)
    for price, (ref_macd, ref_signal) in zip(prices, ref):
        engine.update(price)
        assert engine.macd == pytest.approx(ref_macd)
        assert engine.signal == pytest.approx(ref_signal)


def test_warmup_gating_returns_none_before_enough_ticks():
    engine = TickMacdEngine(fast_period=2, slow_period=3, signal_period=2)  # warm-up = 5 ticks
    results = [engine.update(p) for p in [100, 101, 102, 103]]
    assert all(r == "NONE" for r in results)
    assert engine.is_warmed_up is False
    engine.update(104)
    assert engine.is_warmed_up is True


def test_current_side_reflects_macd_sign():
    engine = TickMacdEngine(fast_period=2, slow_period=3, signal_period=2)
    for p in [100, 90, 80, 70, 60]:  # sharp downtrend
        engine.update(p)
    assert engine.current_side == "BEARISH"
    assert engine.macd < 0

    for p in [70, 90, 120, 150, 200]:  # sharp reversal upward
        engine.update(p)
    assert engine.current_side == "BULLISH"
    assert engine.macd > 0


# --- Crossover classification (direct state injection) --------------------------------
#
# Hand-deriving a natural price sequence that lands a crossover exactly above/below zero is
# impractical, so these tests inject internal EMA state directly (alpha=0.5 for all three
# EMAs, i.e. period=3, chosen so `ema_fast - ema_slow` after one more update() depends only
# on the pre-set (ema_fast, ema_slow) gap, not on the next tick's price - fully worked out
# by hand, see comments). This isolates the "which side of zero did the crossover happen
# on" filter from the EMA recursion itself (already covered above).


def _warmed_up_engine() -> TickMacdEngine:
    engine = TickMacdEngine(fast_period=3, slow_period=3, signal_period=3)
    engine._tick_count = 1000
    return engine


def test_bullish_crossover_above_zero_is_reported():
    engine = _warmed_up_engine()
    engine._ema_fast, engine._ema_slow = 2.0, 1.0  # next macd = 0.5*(2-1) = 0.5
    engine._signal_ema = -0.1  # next signal = 0.5*(-0.1) + 0.5*0.5 = 0.2
    engine._prev_macd, engine._prev_signal = -1.0, 0.0  # was below (crossing up)
    result = engine.update(999.0)
    assert engine.macd == pytest.approx(0.5)
    assert engine.signal == pytest.approx(0.2)
    assert result == "BULLISH"


def test_bullish_crossover_below_zero_is_not_reported():
    """MACD crosses above Signal, but the crossover point is still negative - per the
    user's confirmed definition, this does NOT count as a signal."""
    engine = _warmed_up_engine()
    engine._ema_fast, engine._ema_slow = 0.0, 1.0  # next macd = 0.5*(0-1) = -0.5
    engine._signal_ema = -2.5  # next signal = 0.5*(-2.5) + 0.5*(-0.5) = -1.5
    engine._prev_macd, engine._prev_signal = -2.0, -1.0  # was below (crossing up)
    result = engine.update(999.0)
    assert engine.macd == pytest.approx(-0.5)
    assert engine.signal == pytest.approx(-1.5)
    assert engine.macd > engine.signal  # a real crossover did happen...
    assert result == "NONE"  # ...but it's filtered out (wrong side of zero)


def test_bearish_crossover_below_zero_is_reported():
    engine = _warmed_up_engine()
    engine._ema_fast, engine._ema_slow = 1.0, 1.6  # next macd = 0.5*(1-1.6) = -0.3
    engine._signal_ema = 0.1  # next signal = 0.5*0.1 + 0.5*(-0.3) = -0.1
    engine._prev_macd, engine._prev_signal = 1.0, 0.0  # was above (crossing down)
    result = engine.update(999.0)
    assert engine.macd == pytest.approx(-0.3)
    assert engine.signal == pytest.approx(-0.1)
    assert result == "BEARISH"


def test_bearish_crossover_above_zero_is_not_reported():
    engine = _warmed_up_engine()
    engine._ema_fast, engine._ema_slow = 1.6, 1.0  # next macd = 0.5*(1.6-1) = 0.3
    engine._signal_ema = 0.7  # next signal = 0.5*0.7 + 0.5*0.3 = 0.5
    engine._prev_macd, engine._prev_signal = 1.0, 0.0  # was above (crossing down)
    result = engine.update(999.0)
    assert engine.macd == pytest.approx(0.3)
    assert engine.signal == pytest.approx(0.5)
    assert engine.macd < engine.signal  # a real crossover did happen...
    assert result == "NONE"  # ...but it's filtered out (wrong side of zero)
