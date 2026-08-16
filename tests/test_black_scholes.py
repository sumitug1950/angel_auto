import pytest

from angel_auto.analytics.black_scholes import bs_greeks, bs_price

# Textbook reference case (Hull): S=100, K=100, T=1y, r=5%, sigma=20%
# Call ~= 10.4506, Put ~= 5.5735, Call delta ~= 0.6368
S, K, T, R, IV = 100.0, 100.0, 1.0, 0.05, 0.20


def test_call_price_matches_textbook_reference():
    price = bs_price(S, K, T, R, IV, "CE")
    assert price == pytest.approx(10.4506, abs=0.01)


def test_put_price_matches_textbook_reference():
    price = bs_price(S, K, T, R, IV, "PE")
    assert price == pytest.approx(5.5735, abs=0.01)


def test_put_call_parity():
    call = bs_price(S, K, T, R, IV, "CE")
    put = bs_price(S, K, T, R, IV, "PE")
    # C - P = S - K*e^(-rT)
    import math

    assert (call - put) == pytest.approx(S - K * math.exp(-R * T), abs=0.01)


def test_call_delta_matches_textbook_reference():
    greeks = bs_greeks(S, K, T, R, IV, "CE")
    assert greeks.delta == pytest.approx(0.6368, abs=0.01)


def test_put_delta_is_call_delta_minus_one():
    call_greeks = bs_greeks(S, K, T, R, IV, "CE")
    put_greeks = bs_greeks(S, K, T, R, IV, "PE")
    assert put_greeks.delta == pytest.approx(call_greeks.delta - 1.0, abs=1e-9)


def test_deep_itm_call_delta_near_one():
    greeks = bs_greeks(200.0, 100.0, T, R, IV, "CE")
    assert greeks.delta > 0.95


def test_deep_otm_call_delta_near_zero():
    greeks = bs_greeks(50.0, 100.0, T, R, IV, "CE")
    assert greeks.delta < 0.05
