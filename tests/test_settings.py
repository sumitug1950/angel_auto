import pytest
from pydantic import ValidationError

from angel_auto.settings import AppConfig, StrategiesFile, StrategyConfig, get_settings

RISK = {"daily_loss_limit_rs": 8000, "max_consecutive_losses": 2, "max_trades_per_day": 2}


def test_settings_load_and_validate():
    settings = get_settings()

    assert settings.app.lot_size > 0
    assert settings.app.risk.max_trades_per_day == 2

    active = settings.strategies.active
    assert active.candle_interval_sec == 15
    assert active.macd.fast_period == 12
    assert active.exit.sl_amount_rs == 4000
    assert active.exit.target_amount_rs == active.exit.sl_amount_rs * active.exit.risk_reward_ratio
    assert active.sizing.lots == 1
    assert active.expiry_choices == 4
    assert active.buying.strike_grid == 100.0


def test_misspelled_setting_is_rejected_not_silently_ignored():
    with pytest.raises(ValidationError, match="sl_amount"):
        StrategyConfig.model_validate({"class_path": "x", "exit": {"sl_amount": 500}})


def test_otm_delta_must_be_further_out_than_itm():
    with pytest.raises(ValidationError, match="otm_delta"):
        StrategyConfig.model_validate({"class_path": "x", "buying": {"itm_delta": 0.3, "otm_delta": 0.5}})


def test_invalid_choice_is_rejected():
    with pytest.raises(ValidationError, match="on_rise"):
        StrategyConfig.model_validate({"class_path": "x", "vix_override": {"on_rise": "MAYBE"}})


def test_too_many_live_option_prices_for_one_feed_connection_is_rejected():
    with pytest.raises(ValidationError, match="expiry_choices"):
        StrategyConfig.model_validate({"class_path": "x", "expiry_choices": 6, "selling": {"strike_grid": 50}})


def test_bad_time_format_is_rejected():
    with pytest.raises(ValidationError, match="normal_time"):
        AppConfig.model_validate({"risk": RISK, "square_off": {"normal_time": "3:15pm"}})


def test_negative_amount_is_rejected():
    with pytest.raises(ValidationError, match="daily_loss_limit_rs"):
        AppConfig.model_validate({"risk": {**RISK, "daily_loss_limit_rs": -1}})


def test_active_strategy_must_have_a_block():
    with pytest.raises(ValidationError, match="active_strategy"):
        StrategiesFile.model_validate({"active_strategy": "missing", "strategies": {}})
