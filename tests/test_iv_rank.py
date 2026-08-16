import pytest

from angel_auto.analytics.iv_rank import compute_iv_rank


def test_rank_at_minimum_is_zero():
    assert compute_iv_rank(10.0, [10.0, 12.0, 15.0, 20.0]) == pytest.approx(0.0)


def test_rank_at_maximum_is_hundred():
    assert compute_iv_rank(20.0, [10.0, 12.0, 15.0, 20.0]) == pytest.approx(100.0)


def test_rank_at_midpoint():
    assert compute_iv_rank(15.0, [10.0, 20.0]) == pytest.approx(50.0)


def test_rank_clamped_when_current_below_historical_min():
    # current VIX lower than anything in the window - shouldn't go negative
    assert compute_iv_rank(5.0, [10.0, 20.0]) == pytest.approx(0.0)


def test_rank_clamped_when_current_above_historical_max():
    assert compute_iv_rank(30.0, [10.0, 20.0]) == pytest.approx(100.0)


def test_empty_history_raises():
    with pytest.raises(ValueError):
        compute_iv_rank(15.0, [])


def test_zero_range_history_raises():
    with pytest.raises(ValueError):
        compute_iv_rank(15.0, [12.0, 12.0, 12.0])
