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


class AppConfig(BaseModel):
    mode: Mode = Mode.PAPER
    timezone: str = "Asia/Kolkata"
    underlying: str = "NIFTY"
    lot_size: int = 65
    market_hours: MarketHoursConfig = MarketHoursConfig()
    square_off: SquareOffConfig = SquareOffConfig()
    risk: RiskConfig
    oms: OMSConfig = OMSConfig()
    database: DatabaseConfig = DatabaseConfig()
    logging: LoggingConfig = LoggingConfig()
    dashboard: DashboardConfig = DashboardConfig()


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


class StrikesConfig(BaseModel):
    itm_delta_target: float = Field(0.7, gt=0, lt=1)
    otm_delta_target: float = Field(0.1, gt=0, lt=1)


class ExpiryConfig(BaseModel):
    type: Literal["MONTHLY", "WEEKLY"] = "MONTHLY"
    min_days_gap: int = 10


class SizingConfig(BaseModel):
    lots: int = 1


class ExitConfig(BaseModel):
    sl_amount_rs: float = 4000
    risk_reward_ratio: float = 1.2
    trail_gap_rs: float = 1000
    exit_on_opposite_macd: bool = True

    @property
    def target_amount_rs(self) -> float:
        return self.sl_amount_rs * self.risk_reward_ratio


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


class StrategiesFile(BaseModel):
    active_strategy: str
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
