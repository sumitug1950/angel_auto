from angel_auto.backtest.engine import BacktestResult
from angel_auto.backtest.report import compute_report


def _result(trades, equity_values, starting_capital=20000.0):
    equity_curve = [{"total_equity_rs": v} for v in equity_values]
    ending = equity_values[-1] if equity_values else starting_capital
    return BacktestResult(trades=trades, equity_curve=equity_curve, starting_capital_rs=starting_capital, ending_equity_rs=ending)


def test_win_rate_and_pnl():
    trades = [
        {"realized_pnl_rs": 1000.0}, {"realized_pnl_rs": -500.0}, {"realized_pnl_rs": 2000.0},
    ]
    result = _result(trades, [20000, 21000, 20500, 22500])
    report = compute_report(result)
    assert report.total_trades == 3
    assert report.winning_trades == 2
    assert report.losing_trades == 1
    assert report.win_rate_pct == round(2 / 3 * 100, 1)
    assert report.total_pnl_rs == 2500.0


def test_no_trades_gives_zero_metrics():
    result = _result([], [20000.0])
    report = compute_report(result)
    assert report.total_trades == 0
    assert report.win_rate_pct == 0.0


def test_max_drawdown_computed_correctly():
    # peak 25000, trough 20000 -> drawdown 5000 (20%)
    result = _result([], [20000, 22000, 25000, 21000, 20000, 23000])
    report = compute_report(result)
    assert report.max_drawdown_rs == 5000.0
    assert report.max_drawdown_pct == 20.0


def test_return_pct():
    result = _result([], [20000, 25000], starting_capital=20000.0)
    report = compute_report(result)
    assert report.return_pct == 25.0


def test_sharpe_zero_for_flat_equity():
    result = _result([], [20000, 20000, 20000, 20000])
    report = compute_report(result)
    assert report.sharpe_ratio == 0.0


def test_sharpe_positive_for_steady_gains():
    result = _result([], [20000, 20100, 20200, 20300, 20400, 20500])
    report = compute_report(result)
    assert report.sharpe_ratio > 0
