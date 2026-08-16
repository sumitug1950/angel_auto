"""Implied volatility inversion: given a market premium, back-solve the IV that produces it
under Black-Scholes. Used for per-option Greeks (strike selection), not for IV Rank.
"""
from __future__ import annotations

from scipy.optimize import brentq

from angel_auto.analytics.black_scholes import OptionType, bs_price

MIN_IV = 0.001   # 0.1%
MAX_IV = 5.0     # 500% - generous upper bound, real Nifty IV rarely exceeds ~1.0


class IVSolverError(ValueError):
    pass


def solve_iv(
    market_price: float,
    spot: float,
    strike: float,
    time_to_expiry_years: float,
    rate: float,
    option_type: OptionType,
) -> float:
    """Returns the IV (as a decimal, e.g. 0.14 for 14%) implied by market_price.

    Raises IVSolverError if market_price is outside the range of prices achievable at
    any IV in [MIN_IV, MAX_IV] (e.g. a stale/bad quote, or an option with ~zero time value).
    """
    if market_price <= 0:
        raise IVSolverError(f"market_price must be positive, got {market_price}")

    def price_diff(iv: float) -> float:
        return bs_price(spot, strike, time_to_expiry_years, rate, iv, option_type) - market_price

    low, high = price_diff(MIN_IV), price_diff(MAX_IV)
    if low > 0 or high < 0:
        raise IVSolverError(
            f"market_price={market_price} not reachable for spot={spot} strike={strike} "
            f"T={time_to_expiry_years} within IV bounds [{MIN_IV}, {MAX_IV}]"
        )

    return brentq(price_diff, MIN_IV, MAX_IV, xtol=1e-6)
