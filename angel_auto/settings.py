"""Typed config loading: config/.env (secrets) + config/config.yaml (app) + config/strategies.yaml (strategy).

Nothing here talks to the broker or the filesystem beyond reading these three files - it just
produces validated, typed objects everything else depends on.
"""
from __future__ import annotations

from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"


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


class MarketHoursConfig(BaseModel):
    open: str = "09:15"
    close: str = "15:30"


class SquareOffConfig(BaseModel):
    normal_time: str = "15:15"
    expiry_day_time: str = "15:00"


class SchedulerConfig(BaseModel):
    daily_relogin_time: str = "08:45"  # before market open (09:15) - session tokens don't persist overnight


class PaperTradingConfig(BaseModel):
    starting_capital_rs: float = 20000.0  # only used to simulate margin checks in PaperBroker


class RiskConfig(BaseModel):
    daily_loss_limit_rs: float
    max_consecutive_losses: int
    max_trades_per_day: int
    max_lots_per_trade: int
    max_concurrent_positions: int
    max_slippage_pct: float
    api_failure_threshold: int
    api_failure_window_sec: int


class OMSConfig(BaseModel):
    product_type: str = "INTRADAY"
    entry_slippage_buffer_pts: float = 1.0
    safety_net_sl_buffer_pts: float = 5.0


class DatabaseConfig(BaseModel):
    url: str = f"sqlite:///{(REPO_ROOT / 'data_store' / 'angel_auto.db').as_posix()}"


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str = "data_store/logs/angel_auto.log"


class DashboardConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000


class ChargesConfig(BaseModel):
    """Estimated Indian F&O brokerage + statutory charges (analytics/charges.py). Illustrative
    defaults - not a guaranteed match to any specific broker's exact current tariff; edit
    these to match a real contract note before trusting net-P&L figures for any decision."""

    brokerage_per_order_rs: float = 40.0  # flat per executed order (entry + exit = x2 per trade)
    stt_sell_pct: float = 0.1
    exchange_txn_pct: float = 0.03503
    gst_pct: float = 18.0
    sebi_charges_pct: float = 0.0001
    stamp_duty_buy_pct: float = 0.003


class TickRecorderConfig(BaseModel):
    enabled: bool = True


class AppConfig(BaseModel):
    mode: Mode = Mode.PAPER
    timezone: str = "Asia/Kolkata"
    underlying: str = "NIFTY"
    lot_size: int = 65
    risk_free_rate: float = 0.065  # ~ current Indian T-bill/repo yield; minor factor for short-dated options
    market_hours: MarketHoursConfig = MarketHoursConfig()
    square_off: SquareOffConfig = SquareOffConfig()
    scheduler: SchedulerConfig = SchedulerConfig()
    paper_trading: PaperTradingConfig = PaperTradingConfig()
    risk: RiskConfig
    oms: OMSConfig = OMSConfig()
    database: DatabaseConfig = DatabaseConfig()
    logging: LoggingConfig = LoggingConfig()
    dashboard: DashboardConfig = DashboardConfig()
    charges: ChargesConfig = ChargesConfig()
    tick_recorder: TickRecorderConfig = TickRecorderConfig()


class MacdConfig(BaseModel):
    fast_period: int = 12
    slow_period: int = 26
    signal_period: int = 9


class PendingRequestConfig(BaseModel):
    auto_cancel: bool = False


class StructureConfig(BaseModel):
    default: Literal["DEBIT", "CREDIT"] = "DEBIT"
    credit_iv_rank_threshold: float = 50.0
    iv_rank_lookback_days: int = 90
    iv_source: Literal["INDIA_VIX"] = "INDIA_VIX"
    vix_spike_pct_threshold: float = 3.0


class StrikesConfig(BaseModel):
    itm_delta_target: float = Field(0.7, gt=0, lt=1)
    otm_delta_target: float = Field(0.1, gt=0, lt=1)
    strike_grid: float = 100.0


class ExpiryConfig(BaseModel):
    # DEBIT (ITM buy) -> monthly, needs time for the long premium to work.
    # CREDIT (ITM sell) -> nearest/current expiry, benefits from fast theta decay.
    debit_expiry_type: Literal["MONTHLY"] = "MONTHLY"
    debit_min_days_gap: int = 10
    credit_expiry_type: Literal["NEAREST"] = "NEAREST"


class SizingConfig(BaseModel):
    lots: int = 1


class ExitConfig(BaseModel):
    sl_amount_rs: float = 4000
    risk_reward_ratio: float = 1.2
    trail_gap_rs: float = 1000
    exit_on_opposite_macd: bool = False

    @property
    def target_amount_rs(self) -> float:
        return self.sl_amount_rs * self.risk_reward_ratio


class SingleLegConfig(BaseModel):
    """Only set for the automatic tick-driven zero-cross strategies (see
    strategy/macd_zero_cross_single_leg.py) - None for the flagship spread strategy."""

    side: Literal["BUY", "SELL"]
    strike_grid: float = 50.0  # real Nifty strike spacing, not the flagship's coarser 100-pt grid
    itm_offset_count: int = 0  # 0 = ATM; N = N strikes in-the-money from ATM


class EntryWindowConfig(BaseModel):
    start: str = "09:15"
    end: str = "15:15"


class ZeroCrossExitConfig(BaseModel):
    sl_amount_rs: float = 500.0


class StrategyRiskOverrideConfig(BaseModel):
    """Per-strategy override of config.yaml's global risk.* - used by the zero-cross
    strategies, which trade far smaller SL amounts than the flagship and (per explicit
    instruction) have no daily trade-count cap."""

    daily_loss_limit_rs: float = 1500.0
    max_consecutive_losses: int = 3
    max_trades_per_day: int = 100_000  # effectively unbounded - re-enter on every fresh signal


class StrategyConfig(BaseModel):
    class_path: str
    candle_interval_sec: int = 15
    macd: MacdConfig = MacdConfig()
    pending_request: PendingRequestConfig = PendingRequestConfig()
    structure: StructureConfig = StructureConfig()
    strikes: StrikesConfig = StrikesConfig()
    expiry: ExpiryConfig = ExpiryConfig()
    sizing: SizingConfig = SizingConfig()
    exit: ExitConfig = ExitConfig()
    single_leg: SingleLegConfig | None = None
    entry_window: EntryWindowConfig = EntryWindowConfig()
    zero_cross_exit: ZeroCrossExitConfig = ZeroCrossExitConfig()
    risk_override: StrategyRiskOverrideConfig = StrategyRiskOverrideConfig()


class StrategiesFile(BaseModel):
    active_strategy: str
    flagship_enabled: bool = True
    zero_cross_strategies: list[str] = Field(default_factory=list)
    strategies: dict[str, StrategyConfig]

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
