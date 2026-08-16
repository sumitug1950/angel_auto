"""Backtest performance metrics - equity curve stats, Sharpe/Sortino, max drawdown, win
rate. Pure functions over BacktestResult data, no DB/broker dependency, so easy to unit
test with synthetic series.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from angel_auto.backtest.engine import BacktestResult


@dataclass
class PerformanceReport:
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate_pct: float
    total_pnl_rs: float
    max_drawdown_rs: float
    max_drawdown_pct: float
    sharpe_ratio: float
    starting_capital_rs: float
    ending_equity_rs: float
    return_pct: float


def compute_report(result: BacktestResult, risk_free_rate_annual: float = 0.065) -> PerformanceReport:
    trades = result.trades
    total_trades = len(trades)
    wins = [t for t in trades if (t["realized_pnl_rs"] or 0) > 0]
    losses = [t for t in trades if (t["realized_pnl_rs"] or 0) <= 0]
    win_rate = (len(wins) / total_trades * 100.0) if total_trades else 0.0
    total_pnl = sum(t["realized_pnl_rs"] or 0.0 for t in trades)

    equity_values = [p["total_equity_rs"] for p in result.equity_curve]
    max_dd_rs, max_dd_pct = _max_drawdown(equity_values)
    sharpe = _sharpe_ratio(equity_values, risk_free_rate_annual)

    ending_equity = result.ending_equity_rs
    return_pct = (
        (ending_equity - result.starting_capital_rs) / result.starting_capital_rs * 100.0
        if result.starting_capital_rs
        else 0.0
    )

    return PerformanceReport(
        total_trades=total_trades,
        winning_trades=len(wins),
        losing_trades=len(losses),
        win_rate_pct=round(win_rate, 1),
        total_pnl_rs=round(total_pnl, 2),
        max_drawdown_rs=round(max_dd_rs, 2),
        max_drawdown_pct=round(max_dd_pct, 2),
        sharpe_ratio=round(sharpe, 2),
        starting_capital_rs=result.starting_capital_rs,
        ending_equity_rs=round(ending_equity, 2),
        return_pct=round(return_pct, 2),
    )


def _max_drawdown(equity_values: list[float]) -> tuple[float, float]:
    if not equity_values:
        return 0.0, 0.0
    peak = equity_values[0]
    max_dd_rs = 0.0
    max_dd_pct = 0.0
    for value in equity_values:
        peak = max(peak, value)
        drawdown = peak - value
        if drawdown > max_dd_rs:
            max_dd_rs = drawdown
            max_dd_pct = (drawdown / peak * 100.0) if peak else 0.0
    return max_dd_rs, max_dd_pct


def _sharpe_ratio(equity_values: list[float], risk_free_rate_annual: float, periods_per_year: int = 252) -> float:
    if len(equity_values) < 2:
        return 0.0
    returns = [
        (equity_values[i] - equity_values[i - 1]) / equity_values[i - 1]
        for i in range(1, len(equity_values))
        if equity_values[i - 1] != 0
    ]
    if len(returns) < 2:
        return 0.0
    mean_return = sum(returns) / len(returns)
    variance = sum((r - mean_return) ** 2 for r in returns) / (len(returns) - 1)
    std_dev = math.sqrt(variance)
    if std_dev == 0:
        return 0.0
    daily_risk_free = risk_free_rate_annual / periods_per_year
    return (mean_return - daily_risk_free) / std_dev * math.sqrt(periods_per_year)
