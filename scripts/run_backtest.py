"""Runs a backtest over real historical NIFTY spot + India VIX daily data.

Daily granularity, synthesized expiries/strikes, Black-Scholes theoretical option prices -
see angel_auto/backtest/engine.py and data_loader.py docstrings for exactly why and what
that means for how to read the results (mechanics validation, not an exact live replay).

Usage:
    .venv\\Scripts\\python.exe scripts\\run_backtest.py [from_date] [to_date]
    .venv\\Scripts\\python.exe scripts\\run_backtest.py 2026-02-01 2026-08-14
"""
from __future__ import annotations

import sys
from datetime import date, datetime

from angel_auto.backtest.data_loader import load_backtest_series
from angel_auto.backtest.engine import BacktestEngine
from angel_auto.backtest.report import compute_report
from angel_auto.broker.angelone_auth import login, logout
from angel_auto.data.instruments import InstrumentMaster
from angel_auto.logging_conf import configure_logging
from angel_auto.persistence.db import init_db
from angel_auto.settings import get_settings


def main() -> int:
    configure_logging()
    init_db()
    settings = get_settings()

    if len(sys.argv) >= 3:
        from_date = datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
        to_date = datetime.strptime(sys.argv[2], "%Y-%m-%d").date()
    else:
        to_date = date.today()
        from_date = to_date.replace(month=max(to_date.month - 6, 1)) if to_date.month > 6 else date(to_date.year - 1, to_date.month + 6, 1)

    print(f"Backtesting {from_date} to {to_date}...")
    print("Logging in to fetch historical data...")
    session = login(settings.credentials)

    instruments = InstrumentMaster()
    instruments.load()
    spot = instruments.nifty_spot_historical_instrument()  # different token than the live-feed one
    vix = instruments.india_vix_instrument()

    series = load_backtest_series(session, spot.token, vix.token, from_date, to_date)
    logout(session)
    print(f"Loaded {len(series)} trading days.")
    if not series:
        print("No historical data available for this range.")
        return 1

    engine = BacktestEngine(
        strategy_config=settings.strategies.active,
        underlying=settings.app.underlying,
        lot_size=settings.app.lot_size,
        max_trades_per_day=settings.app.risk.max_trades_per_day,
        daily_loss_limit_rs=settings.app.risk.daily_loss_limit_rs,
        max_consecutive_losses=settings.app.risk.max_consecutive_losses,
        rate=settings.app.risk_free_rate,
        starting_capital_rs=settings.app.paper_trading.starting_capital_rs,
    )
    result = engine.run(series)
    report = compute_report(result, settings.app.risk_free_rate)

    print()
    print("=== Backtest Report ===")
    print(f"Period:              {from_date} to {to_date} ({len(series)} trading days)")
    print(f"Starting capital:    Rs {report.starting_capital_rs:,.0f}")
    print(f"Ending equity:       Rs {report.ending_equity_rs:,.0f}")
    print(f"Return:              {report.return_pct:+.2f}%")
    print(f"Total P&L:           Rs {report.total_pnl_rs:,.0f}")
    print(f"Total trades:        {report.total_trades}")
    print(f"Win rate:            {report.win_rate_pct:.1f}% ({report.winning_trades}W / {report.losing_trades}L)")
    print(f"Max drawdown:        Rs {report.max_drawdown_rs:,.0f} ({report.max_drawdown_pct:.1f}%)")
    print(f"Sharpe ratio:        {report.sharpe_ratio:.2f}")
    print()
    print("Reminder: daily bars + synthesized expiries/strikes + Black-Scholes theoretical")
    print("option prices - this validates strategy mechanics, not exact live timing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
