"""Typed config loading: config/.env (secrets) + config/config.yaml (app) + config/strategies.yaml (strategy).

Nothing here talks to the broker or the filesystem beyond reading these three files - it just
produces validated, typed objects everything else depends on.

Every YAML-backed section forbids unknown keys and range-checks its values: a misspelled or
out-of-range setting fails loudly at startup (scripts/check_config.py shows exactly which one)
instead of silently falling back to a default.
"""
from __future__ import annotations

from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"

HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"  # "09:15"
Button = Literal["BUYING", "SELLING"]


class Mode(str, Enum):
    PAPER = "paper"
    LIVE = "live"
    BACKTEST = "backtest"


class BrokerCredentials(BaseSettings):
    """Loaded from config/.env - never hardcode these, never log them."""

    model_config = SettingsConfigDict(
        env_file=str(CONFIG_DIR / ".env"),
        env_prefix="ANGEL_",
        extra="ignore",
    )

    api_key: str = ""
    client_code: str = ""
    mpin: str = ""
    totp_secret: str = ""
    market_api_key: str = ""


class ConfigModel(BaseModel):
    """Base for every YAML-backed section - an unknown (e.g. misspelled) key is an error."""

    model_config = ConfigDict(extra="forbid")


# --- config/config.yaml ---------------------------------------------------------------


class MarketHoursConfig(ConfigModel):
    open: str = Field("09:15", pattern=HHMM)
    close: str = Field("15:30", pattern=HHMM)


class SquareOffConfig(ConfigModel):
    normal_time: str = Field("15:15", pattern=HHMM)
    expiry_day_time: str = Field("15:00", pattern=HHMM)


class SchedulerConfig(ConfigModel):
    daily_relogin_time: str = Field("08:45", pattern=HHMM)  # before market open - session tokens don't persist overnight
    daily_reset_time: str = Field("08:30", pattern=HHMM)  # cancels a Pending request left over from a previous day
    eod_vix_capture_time: str = Field("15:35", pattern=HHMM)  # records the day's India VIX close


class PaperTradingConfig(ConfigModel):
    starting_capital_rs: float = Field(20000.0, gt=0)  # only used to simulate margin checks in PaperBroker
    slippage_pct: float = Field(0.1, ge=0)
    simulate_margin_check: bool = False  # live mode always checks margin with the broker


class RiskConfig(ConfigModel):
    daily_loss_limit_rs: float = Field(gt=0)
    max_consecutive_losses: int = Field(gt=0)
    max_trades_per_day: int = Field(gt=0)


class OMSConfig(ConfigModel):
    product_type: str = "INTRADAY"
    entry_slippage_buffer_pts: float = Field(1.0, ge=0)
    entry_retry_attempts: int = Field(3, ge=0)
    entry_retry_delay_sec: float = Field(1.0, ge=0)
    fill_timeout_sec: float = Field(10.0, gt=0)  # wait this long for the broker to confirm a fill, then cancel
    fill_poll_interval_sec: float = Field(2.0, gt=0)  # Angel One's order book throttles ~1 request/sec ("exceeding access rate")
    exit_attempts: int = Field(3, ge=1)  # re-sends of a partially filled exit's remainder
    sl_limit_buffer_pts: float = Field(5.0, ge=0)  # broker backup SL: limit price = trigger + this


class MarketDataConfig(ConfigModel):
    greeks_refresh_interval_sec: float = Field(5.0, gt=0)


class DatabaseConfig(ConfigModel):
    url: str = f"sqlite:///{(REPO_ROOT / 'data_store' / 'angel_auto.db').as_posix()}"


class LoggingConfig(ConfigModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    file: str = "data_store/logs/angel_auto.log"


class DashboardConfig(ConfigModel):
    host: str = "0.0.0.0"
    port: int = Field(8000, gt=0, lt=65536)


class ChargesConfig(ConfigModel):
    """Estimated Indian F&O brokerage + statutory charges (analytics/charges.py). Illustrative
    defaults - not a guaranteed match to any specific broker's exact current tariff; edit
    these to match a real contract note before trusting net-P&L figures for any decision."""

    brokerage_per_order_rs: float = Field(40.0, ge=0)  # flat per executed order (entry + exit = x2 per trade)
    stt_sell_pct: float = Field(0.1, ge=0)
    exchange_txn_pct: float = Field(0.03503, ge=0)
    gst_pct: float = Field(18.0, ge=0)
    sebi_charges_pct: float = Field(0.0001, ge=0)
    stamp_duty_buy_pct: float = Field(0.003, ge=0)


class TickRecorderConfig(ConfigModel):
    enabled: bool = True


class AppConfig(ConfigModel):
    mode: Mode = Mode.PAPER
    timezone: str = "Asia/Kolkata"
    underlying: str = "NIFTY"
    lot_size: int = Field(65, gt=0)
    risk_free_rate: float = Field(0.065, ge=0)  # ~ current Indian T-bill/repo yield; minor factor for short-dated options
    market_hours: MarketHoursConfig = Field(default_factory=MarketHoursConfig)
    square_off: SquareOffConfig = Field(default_factory=SquareOffConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    paper_trading: PaperTradingConfig = Field(default_factory=PaperTradingConfig)
    risk: RiskConfig
    oms: OMSConfig = Field(default_factory=OMSConfig)
    market_data: MarketDataConfig = Field(default_factory=MarketDataConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    charges: ChargesConfig = Field(default_factory=ChargesConfig)
    tick_recorder: TickRecorderConfig = Field(default_factory=TickRecorderConfig)


# --- config/strategies.yaml -----------------------------------------------------------


class MacdConfig(ConfigModel):
    fast_period: int = Field(12, gt=0)
    slow_period: int = Field(26, gt=0)
    signal_period: int = Field(9, gt=0)
    min_candles_before_entry: int = Field(0, ge=0)  # 0 = no warm-up gate

    @model_validator(mode="after")
    def _fast_below_slow(self) -> MacdConfig:
        if self.fast_period >= self.slow_period:
            raise ValueError("fast_period must be smaller than slow_period")
        return self


class LegRulesConfig(ConfigModel):
    """Expiry + strike rules for one dashboard button (strategies.yaml `buying:` / `selling:`)."""

    expiry: Literal["WEEKLY", "MONTHLY"]
    min_days_to_expiry: int = Field(0, ge=0)  # skip to the next expiry if fewer days than this remain
    strike_grid: float = Field(100.0, gt=0)
    itm_delta: float = Field(0.7, gt=0, lt=1)
    otm_delta: float = Field(0.1, gt=0, lt=1)

    @model_validator(mode="after")
    def _otm_further_out_than_itm(self) -> LegRulesConfig:
        if self.otm_delta >= self.itm_delta:
            raise ValueError("otm_delta must be smaller than itm_delta (the OTM strike is further out)")
        return self


class VixOverrideConfig(ConfigModel):
    """India VIX moved at least threshold_pct vs yesterday's close -> force a button (or OFF)."""

    threshold_pct: float = Field(3.0, gt=0)
    on_rise: Literal["BUYING", "SELLING", "OFF"] = "BUYING"
    on_fall: Literal["BUYING", "SELLING", "OFF"] = "SELLING"


class SizingConfig(ConfigModel):
    lots: int = Field(1, gt=0)


class ExitConfig(ConfigModel):
    sl_amount_rs: float = Field(4000, gt=0)
    risk_reward_ratio: float = Field(1.2, gt=0)
    trail_gap_rs: float = Field(1000, gt=0)
    exit_on_opposite_macd: bool = False
    broker_backup_sl: bool = True  # CREDIT trades: stop-loss order resting at the broker on the short leg
    broker_backup_sl_multiple: float = Field(1.5, ge=1.0)  # triggers at this x sl_amount_rs of loss on that leg

    @property
    def target_amount_rs(self) -> float:
        return self.sl_amount_rs * self.risk_reward_ratio


class StrategyConfig(ConfigModel):
    class_path: str
    candle_interval_sec: int = Field(15, gt=0)
    check_interval_sec: float = Field(15.0, gt=0)  # how often exits + Pending requests are evaluated
    macd: MacdConfig = Field(default_factory=MacdConfig)
    start_with: Button = "BUYING"  # button in effect until one has ever been pressed
    buying: LegRulesConfig = Field(default_factory=lambda: LegRulesConfig(expiry="MONTHLY", min_days_to_expiry=10))
    selling: LegRulesConfig = Field(default_factory=lambda: LegRulesConfig(expiry="WEEKLY"))
    option_band_points: float = Field(2500.0, gt=0)  # live quotes subscribed within this many points of spot
    vix_override: VixOverrideConfig = Field(default_factory=VixOverrideConfig)
    iv_rank_lookback_days: int = Field(90, gt=0)  # IV Rank is recorded per trade for reference only
    sizing: SizingConfig = Field(default_factory=SizingConfig)
    exit: ExitConfig = Field(default_factory=ExitConfig)


class StrategiesFile(ConfigModel):
    active_strategy: str
    flagship_enabled: bool = True
    strategies: dict[str, StrategyConfig]

    @model_validator(mode="after")
    def _active_strategy_exists(self) -> StrategiesFile:
        if self.active_strategy not in self.strategies:
            raise ValueError(f"active_strategy '{self.active_strategy}' has no block under strategies:")
        return self

    @property
    def active(self) -> StrategyConfig:
        return self.strategies[self.active_strategy]


class Settings(BaseModel):
    credentials: BrokerCredentials
    app: AppConfig
    strategies: StrategiesFile


def _load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@lru_cache
def get_settings() -> Settings:
    app_config = AppConfig.model_validate(_load_yaml(CONFIG_DIR / "config.yaml"))
    strategies_config = StrategiesFile.model_validate(_load_yaml(CONFIG_DIR / "strategies.yaml"))
    credentials = BrokerCredentials()
    return Settings(credentials=credentials, app=app_config, strategies=strategies_config)
