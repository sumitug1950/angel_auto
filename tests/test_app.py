import pytest

from angel_auto.core.app import LIVE_TRADING_CONFIRM_ENV_VAR, LIVE_TRADING_CONFIRM_VALUE, TradingApp
from angel_auto.settings import Mode, Settings, get_settings


def _settings_with_mode(mode: Mode) -> Settings:
    base = get_settings()
    app_cfg = base.app.model_copy(update={"mode": mode})
    return Settings(credentials=base.credentials, app=app_cfg, strategies=base.strategies)


def test_app_raises_for_unimplemented_backtest_mode():
    with pytest.raises(NotImplementedError):
        TradingApp(_settings_with_mode(Mode.BACKTEST))


def test_app_raises_for_live_mode_without_confirmation_env_var(monkeypatch):
    monkeypatch.delenv(LIVE_TRADING_CONFIRM_ENV_VAR, raising=False)
    with pytest.raises(RuntimeError, match=LIVE_TRADING_CONFIRM_ENV_VAR):
        TradingApp(_settings_with_mode(Mode.LIVE))


def test_app_raises_for_live_mode_with_wrong_confirmation_value(monkeypatch):
    monkeypatch.setenv(LIVE_TRADING_CONFIRM_ENV_VAR, "yes")
    with pytest.raises(RuntimeError):
        TradingApp(_settings_with_mode(Mode.LIVE))


def test_app_constructs_in_live_mode_with_correct_confirmation(monkeypatch):
    monkeypatch.setenv(LIVE_TRADING_CONFIRM_ENV_VAR, LIVE_TRADING_CONFIRM_VALUE)
    app = TradingApp(_settings_with_mode(Mode.LIVE))
    assert app.broker is None  # still not wired up until start()


def test_app_constructs_cleanly_in_paper_mode():
    app = TradingApp(get_settings())
    assert app.broker is None  # not wired up until start()
    assert app.strategy is None
