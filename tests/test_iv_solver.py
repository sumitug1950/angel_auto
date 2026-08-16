import pytest

from angel_auto.analytics.black_scholes import bs_price
from angel_auto.analytics.iv_solver import IVSolverError, solve_iv

S, K, T, R = 24800.0, 24600.0, 4 / 365, 0.065


@pytest.mark.parametrize("true_iv", [0.05, 0.12, 0.14, 0.25, 0.60])
def test_round_trip_price_to_iv_to_price(true_iv):
    market_price = bs_price(S, K, T, R, true_iv, "CE")
    solved_iv = solve_iv(market_price, S, K, T, R, "CE")
    assert solved_iv == pytest.approx(true_iv, abs=1e-4)


def test_round_trip_for_put():
    true_iv = 0.18
    market_price = bs_price(S, K, T, R, true_iv, "PE")
    solved_iv = solve_iv(market_price, S, K, T, R, "PE")
    assert solved_iv == pytest.approx(true_iv, abs=1e-4)


def test_unreachable_price_raises():
    with pytest.raises(IVSolverError):
        # a price far above what's achievable at any sane IV
        solve_iv(100000.0, S, K, T, R, "CE")


def test_non_positive_price_raises():
    with pytest.raises(IVSolverError):
        solve_iv(0.0, S, K, T, R, "CE")
