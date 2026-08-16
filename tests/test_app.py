import pytest

from angel_auto.core.app import TradingApp
from angel_auto.settings import AppConfig, Mode, Settings, get_settings


def test_app_raises_for_unimplemented_live_mode():
    base = get_settings()
    live_app_cfg = base.app.model_copy(update={"mode": Mode.LIVE})
    live_settings = Settings(credentials=base.credentials, app=live_app_cfg, strategies=base.strategies)

    with pytest.raises(NotImplementedError):
        TradingApp(live_settings)


def test_app_raises_for_unimplemented_backtest_mode():
    base = get_settings()
    backtest_app_cfg = base.app.model_copy(update={"mode": Mode.BACKTEST})
    backtest_settings = Settings(credentials=base.credentials, app=backtest_app_cfg, strategies=base.strategies)

    with pytest.raises(NotImplementedError):
        TradingApp(backtest_settings)


def test_app_constructs_cleanly_in_paper_mode():
    app = TradingApp(get_settings())
    assert app.broker is None  # not wired up until start()
    assert app.strategy is None
