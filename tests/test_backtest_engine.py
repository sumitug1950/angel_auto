from datetime import date, timedelta

import pytest

from angel_auto.backtest.data_loader import DailyBar
from angel_auto.backtest.engine import BacktestEngine, _synthetic_monthly_expiry, _synthetic_nearest_expiry
from angel_auto.settings import get_settings


def test_synthetic_monthly_expiry_uses_current_month_when_gap_sufficient():
    expiry = _synthetic_monthly_expiry(date(2026, 8, 1), min_days_gap=10)
    assert expiry == "31AUG2026"  # last day of August


def test_synthetic_monthly_expiry_rolls_to_next_month_when_too_close():
    expiry = _synthetic_monthly_expiry(date(2026, 8, 25), min_days_gap=10)  # only 6 days left in Aug
    assert expiry == "30SEP2026"  # last day of September


def test_synthetic_nearest_expiry_is_one_week_out():
    expiry = _synthetic_nearest_expiry(date(2026, 8, 1))
    assert expiry == "08AUG2026"


def _build_series(start: date, days: int, prices: list[float]) -> list[DailyBar]:
    assert len(prices) == days
    series = []
    d = start
    for price in prices:
        # skip weekends for realism, not that the engine cares
        while d.weekday() >= 5:
            d += timedelta(days=1)
        series.append(DailyBar(trade_date=d, spot_close=price, vix_close=15.0))
        d += timedelta(days=1)
    return series


@pytest.fixture
def engine():
    settings = get_settings()
    strat_cfg = settings.strategies.active
    return BacktestEngine(
        strategy_config=strat_cfg,
        underlying="NIFTY",
        lot_size=65,
        max_trades_per_day=settings.app.risk.max_trades_per_day,
        daily_loss_limit_rs=settings.app.risk.daily_loss_limit_rs,
        max_consecutive_losses=settings.app.risk.max_consecutive_losses,
        rate=settings.app.risk_free_rate,
        starting_capital_rs=50000.0,  # generous, so affordability isn't the bottleneck in this test
    )


def test_engine_runs_full_series_without_crashing(engine):
    # a clear uptrend followed by a clear downtrend - should produce at least one crossover
    up = [24000 + i * 40 for i in range(40)]
    down = [up[-1] - i * 40 for i in range(1, 41)]
    series = _build_series(date(2026, 1, 5), 80, up + down)

    result = engine.run(series)

    assert len(result.equity_curve) == len(series)
    assert result.starting_capital_rs == 50000.0
    # not asserting trade count > 0 here - margin/data availability can legitimately mean
    # zero trades on some synthetic runs; the real assertion is that it completes cleanly
    # and produces internally consistent output
    assert isinstance(result.trades, list)


def test_engine_produces_at_least_one_trade_on_strong_trend(engine):
    # a very strong, sustained trend should reliably trigger at least one crossover + entry
    prices = [24000 + i * 60 for i in range(120)]
    series = _build_series(date(2026, 1, 5), 120, prices)

    result = engine.run(series)

    assert len(result.trades) >= 1
    for trade in result.trades:
        assert trade["realized_pnl_rs"] is not None


def test_engine_equity_curve_starts_near_starting_capital(engine):
    prices = [24000 + i * 10 for i in range(30)]
    series = _build_series(date(2026, 1, 5), 30, prices)

    result = engine.run(series)

    first_point = result.equity_curve[0]
    assert abs(first_point["total_equity_rs"] - 50000.0) < 5000.0  # no wild first-day swing


def test_synthetic_strike_tokens_are_stable_across_days(engine):
    """Regression test: an earlier version hashed bar.trade_date into the synthetic token,
    so a position's exit legs silently failed to resolve days after entry (broker-side
    token registry had been overwritten with different tokens for the same conceptual
    strike). Tokens for the same expiry must be identical across two different days."""
    bar1 = DailyBar(trade_date=date(2026, 1, 5), spot_close=24000.0, vix_close=15.0)
    bar2 = DailyBar(trade_date=date(2026, 1, 20), spot_close=24100.0, vix_close=16.0)

    instruments_day1 = engine._build_synthetic_strikes(bar1, "31JAN2026")
    tokens_day1 = {(inst.strike, inst.symbol[-2:]): inst.token for inst in instruments_day1}

    instruments_day2 = engine._build_synthetic_strikes(bar2, "31JAN2026")
    tokens_day2 = {(inst.strike, inst.symbol[-2:]): inst.token for inst in instruments_day2}

    common_keys = set(tokens_day1) & set(tokens_day2)
    assert common_keys, "expected overlapping strikes between the two days"
    for key in common_keys:
        assert tokens_day1[key] == tokens_day2[key], f"token for {key} changed between days"


def test_full_position_survives_multi_day_hold_and_exits_with_nonzero_pnl(engine):
    # a position that must stay open across several daily bars before the exit fires -
    # exercises the exact token-lookup-days-later path the regression above targets
    prices = [24000] * 5 + [24000 + i * 80 for i in range(1, 20)]  # flat, then a sharp trend
    series = _build_series(date(2026, 1, 5), len(prices), prices)

    result = engine.run(series)

    assert len(result.trades) >= 1
    for trade in result.trades:
        assert trade["realized_pnl_rs"] is not None
        # a real fill produces a nonzero P&L virtually always at these price moves;
        # exactly 0.0 across every leg would indicate the exit legs failed to resolve
    assert any(t["realized_pnl_rs"] != 0.0 for t in result.trades)
