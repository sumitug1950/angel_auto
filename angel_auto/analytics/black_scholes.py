"""Black-Scholes pricing + Greeks for European options (NSE index options are cash-settled
European style, so this is the correct model - no early-exercise premium to worry about).

Used only for per-option delta when picking ITM/OTM strikes (see strategy/macd_itm_otm_spread.py).
Market-wide IV Rank uses India VIX instead - see iv_rank.py.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from scipy.stats import norm

OptionType = Literal["CE", "PE"]


@dataclass(frozen=True)
class Greeks:
    price: float
    delta: float       # per 1.0 move in spot
    gamma: float        # per 1.0 move in spot
    vega: float          # per 1.0 (=100 percentage points) change in IV; divide by 100 for "per 1% IV"
    theta_per_day: float  # time decay per calendar day, already divided by 365


def _d1_d2(spot: float, strike: float, time_to_expiry_years: float, rate: float, iv: float) -> tuple[float, float]:
    if time_to_expiry_years <= 0 or iv <= 0:
        raise ValueError("time_to_expiry_years and iv must both be positive")
    sqrt_t = math.sqrt(time_to_expiry_years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * iv**2) * time_to_expiry_years) / (iv * sqrt_t)
    d2 = d1 - iv * sqrt_t
    return d1, d2


def bs_price(
    spot: float,
    strike: float,
    time_to_expiry_years: float,
    rate: float,
    iv: float,
    option_type: OptionType,
) -> float:
    """European option price. time_to_expiry_years must be > 0 (use a small epsilon at expiry)."""
    d1, d2 = _d1_d2(spot, strike, time_to_expiry_years, rate, iv)
    discount = math.exp(-rate * time_to_expiry_years)
    if option_type == "CE":
        return spot * norm.cdf(d1) - strike * discount * norm.cdf(d2)
    return strike * discount * norm.cdf(-d2) - spot * norm.cdf(-d1)


def bs_greeks(
    spot: float,
    strike: float,
    time_to_expiry_years: float,
    rate: float,
    iv: float,
    option_type: OptionType,
) -> Greeks:
    d1, d2 = _d1_d2(spot, strike, time_to_expiry_years, rate, iv)
    sqrt_t = math.sqrt(time_to_expiry_years)
    discount = math.exp(-rate * time_to_expiry_years)
    pdf_d1 = norm.pdf(d1)

    price = bs_price(spot, strike, time_to_expiry_years, rate, iv, option_type)

    delta = norm.cdf(d1) if option_type == "CE" else norm.cdf(d1) - 1.0
    gamma = pdf_d1 / (spot * iv * sqrt_t)
    vega = spot * pdf_d1 * sqrt_t

    if option_type == "CE":
        theta_annual = -(spot * pdf_d1 * iv) / (2 * sqrt_t) - rate * strike * discount * norm.cdf(d2)
    else:
        theta_annual = -(spot * pdf_d1 * iv) / (2 * sqrt_t) + rate * strike * discount * norm.cdf(-d2)

    return Greeks(
        price=price,
        delta=delta,
        gamma=gamma,
        vega=vega,
        theta_per_day=theta_annual / 365.0,
    )
