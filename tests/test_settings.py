from angel_auto.settings import get_settings


def test_settings_load_and_validate():
    settings = get_settings()

    assert settings.app.lot_size > 0
    assert settings.app.risk.max_trades_per_day == 2
    assert settings.app.risk.max_lots_per_trade == 1

    active = settings.strategies.active
    assert active.candle_interval_sec == 15
    assert active.macd.fast_period == 12
    assert active.exit.sl_amount_rs == 4000
    assert active.exit.target_amount_rs == active.exit.sl_amount_rs * active.exit.risk_reward_ratio
    assert active.sizing.lots == 1
