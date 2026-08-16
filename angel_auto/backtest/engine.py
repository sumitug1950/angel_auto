"""Event-driven backtest replay - runs the *exact same* Strategy/OMS/risk code as paper/
live mode (core/app.py), day by day over historical daily bars (see data_loader.py for why
daily, not 15-sec).

Two documented approximations, both because real historical data for them isn't practically
available (see module docstrings in data_loader.py / backtest_broker.py):
  1. Option prices are Black-Scholes theoretical (that day's spot/VIX), not real historical
     per-contract prices.
  2. Expiries/strikes are synthesized by date arithmetic (last day of month for "monthly",
     day+7 for "nearest") rather than looked up from a historical instrument master, since
     the scrip master only reflects currently/future-listed contracts.
  3. There's no human clicking Long/Short in a backtest - every fresh MACD crossover (while
     no position is open) is treated as an immediate direction request, simulating an
     always-attentive user. This tests the strategy's mechanics (structure/expiry/strike
     selection, exit behavior), not real human timing/judgment.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from angel_auto.analytics.black_scholes import bs_greeks
from angel_auto.analytics.indicators import compute_macd, detect_crossover
from angel_auto.backtest.data_loader import DailyBar
from angel_auto.broker.backtest_broker import BacktestBroker
from angel_auto.core.enums import Direction
from angel_auto.data.instruments import Instrument, InstrumentMaster
from angel_auto.data.market_data import BarAggregator, OptionChainSnapshot
from angel_auto.logging_conf import get_logger
from angel_auto.oms.order_manager import OrderManager
from angel_auto.persistence import journal
from angel_auto.risk import pretrade as pretrade_checks
from angel_auto.settings import StrategyConfig
from angel_auto.strategy.macd_itm_otm_spread import MacdItmOtmSpreadStrategy

log = get_logger(__name__)

STRIKE_BAND_POINTS = 1000.0
NEAREST_EXPIRY_DAYS_AHEAD = 7


def _fmt_expiry(d: date) -> str:
    return d.strftime("%d%b%Y").upper()


def _synthetic_monthly_expiry(as_of: date, min_days_gap: int) -> str:
    last_day_this_month = date(as_of.year, as_of.month, calendar.monthrange(as_of.year, as_of.month)[1])
    if (last_day_this_month - as_of).days >= min_days_gap:
        return _fmt_expiry(last_day_this_month)
    next_month = as_of.replace(day=1) + timedelta(days=32)
    last_day_next_month = date(next_month.year, next_month.month, calendar.monthrange(next_month.year, next_month.month)[1])
    return _fmt_expiry(last_day_next_month)


def _synthetic_nearest_expiry(as_of: date) -> str:
    return _fmt_expiry(as_of + timedelta(days=NEAREST_EXPIRY_DAYS_AHEAD))


@dataclass
class BacktestResult:
    trades: list[dict]
    equity_curve: list[dict]
    starting_capital_rs: float
    ending_equity_rs: float


class BacktestEngine:
    def __init__(
        self,
        strategy_config: StrategyConfig,
        underlying: str,
        lot_size: int,
        max_trades_per_day: int,
        daily_loss_limit_rs: float,
        max_consecutive_losses: int,
        rate: float,
        starting_capital_rs: float = 20000.0,
    ) -> None:
        self.strategy_config = strategy_config
        self.underlying = underlying
        self.lot_size = lot_size
        self.max_trades_per_day = max_trades_per_day
        self.daily_loss_limit_rs = daily_loss_limit_rs
        self.max_consecutive_losses = max_consecutive_losses
        self.rate = rate
        self.starting_capital_rs = starting_capital_rs

        self.instruments = InstrumentMaster()
        self.instruments._loaded = True  # populated per-day below; never hits the network
        self.bars = BarAggregator(interval_sec=1)  # unit is irrelevant here - we feed one point per day
        self.option_chain = OptionChainSnapshot()
        self.broker = BacktestBroker(rate=rate, starting_capital_rs=starting_capital_rs)
        # retry_delay_sec=0: OMS's fill-failure retry loop is for live network blips - against
        # a deterministic synthetic price lookup, sleeping real wall-clock seconds per retry
        # just wastes backtest run time for no benefit (retrying won't change the answer).
        self.oms = OrderManager(self.broker, retry_delay_sec=0.0)
        self.strategy = MacdItmOtmSpreadStrategy(
            config=strategy_config,
            underlying=underlying,
            lot_size=lot_size,
            max_trades_per_day=max_trades_per_day,
            instruments=self.instruments,
            bar_aggregator=self.bars,
            option_chain=self.option_chain,
            get_current_vix=lambda: self._current_vix,
            get_today=lambda: self._current_date,
        )
        self._current_vix: float = 0.0
        self._current_date: date = date.today()

    def run(self, series: list[DailyBar]) -> BacktestResult:
        for bar in series:
            self._process_day(bar)

        self._force_close_if_open(series[-1] if series else None)

        trades = journal.list_recent_positions(limit=10000)
        equity_curve = journal.get_equity_curve(limit=10000)
        ending_equity = equity_curve[-1]["total_equity_rs"] if equity_curve else self.starting_capital_rs
        return BacktestResult(
            trades=trades, equity_curve=equity_curve,
            starting_capital_rs=self.starting_capital_rs, ending_equity_rs=ending_equity,
        )

    def _process_day(self, bar: DailyBar) -> None:
        self._current_date = bar.trade_date
        journal.upsert_vix_close(bar.trade_date, bar.vix_close)
        self._current_vix = bar.vix_close

        self.bars.add_tick(bar.spot_close, datetime(bar.trade_date.year, bar.trade_date.month, bar.trade_date.day))
        self.broker.set_market_state(bar.trade_date, bar.spot_close, bar.vix_close)
        self._refresh_synthetic_chain(bar)

        open_position = journal.get_open_position()
        if open_position is not None and bar.trade_date >= datetime.strptime(open_position["expiry"], "%d%b%Y").date():
            intent = self.strategy.on_square_off_trigger()
            if intent is not None:
                self.oms.execute_exit(intent, self.daily_loss_limit_rs, self.max_consecutive_losses, trade_date=bar.trade_date)

        macd_df = compute_macd(
            self.bars.closes_series(),
            self.strategy_config.macd.fast_period,
            self.strategy_config.macd.slow_period,
            self.strategy_config.macd.signal_period,
        )

        if journal.get_open_position() is None and len(macd_df) >= 2:
            crossover = detect_crossover(macd_df)
            if crossover != "NONE" and pretrade_checks.run_pretrade_checks(self.max_trades_per_day, trade_date=bar.trade_date).allowed:
                direction = Direction.LONG if crossover == "BULLISH" else Direction.SHORT
                intent = self.strategy.on_direction_request(direction)
                if intent is not None:
                    self.oms.execute_entry(intent, trade_date=bar.trade_date)

        exit_intent = self.strategy.on_market_data()
        if exit_intent is not None and hasattr(exit_intent, "reason"):
            from angel_auto.strategy.base import ExitIntent

            if isinstance(exit_intent, ExitIntent):
                self.oms.execute_exit(exit_intent, self.daily_loss_limit_rs, self.max_consecutive_losses, trade_date=bar.trade_date)

        self._record_equity_point(bar)

    def _refresh_synthetic_chain(self, bar: DailyBar) -> None:
        monthly_expiry = _synthetic_monthly_expiry(bar.trade_date, self.strategy_config.expiry.debit_min_days_gap)
        nearest_expiry = _synthetic_nearest_expiry(bar.trade_date)

        by_key: dict[tuple[str, str, str], list[Instrument]] = {}
        for expiry in {monthly_expiry, nearest_expiry}:
            instruments = self._build_synthetic_strikes(bar, expiry)
            by_key[(self.underlying, "OPTIDX", expiry)] = instruments
        self.instruments._by_name_type_expiry = by_key

        expiry_date = datetime.strptime(monthly_expiry, "%d%b%Y").date()
        expiry_date_nearest = datetime.strptime(nearest_expiry, "%d%b%Y").date()

        for expiry, exp_date in [(monthly_expiry, expiry_date), (nearest_expiry, expiry_date_nearest)]:
            days_remaining = max((exp_date - bar.trade_date).days, 1)
            t = days_remaining / 365.0
            iv = max(bar.vix_close / 100.0, 0.01)
            for inst in self.instruments._by_name_type_expiry[(self.underlying, "OPTIDX", expiry)]:
                option_type = inst.symbol[-2:]
                self.option_chain.register(inst.token, inst.symbol, inst.strike, option_type, expiry=expiry)
                greeks = bs_greeks(bar.spot_close, inst.strike, t, self.rate, iv, option_type)
                self.option_chain.update_ltp(inst.token, max(greeks.price, 0.05))
                quote = self.option_chain.get(inst.token)
                quote.iv = iv
                quote.delta = greeks.delta
                self.broker.register_token(inst.token, inst.strike, option_type, expiry)

    def _build_synthetic_strikes(self, bar: DailyBar, expiry: str) -> list[Instrument]:
        # IMPORTANT: token must be stable across days for the same (expiry, strike, type) -
        # a position entered on day N needs the same token to still resolve on day N+k when
        # it exits. Deliberately excludes bar.trade_date from the hash (an earlier version
        # didn't, and every day's re-registration silently orphaned every open position's
        # exit - fixed after that showed up as exit legs failing / phantom zero P&L in a
        # real backtest run).
        grid = self.strategy_config.strikes.strike_grid
        center = round(bar.spot_close / grid) * grid
        strikes = [center + i * grid for i in range(int(-STRIKE_BAND_POINTS // grid), int(STRIKE_BAND_POINTS // grid) + 1)]
        instruments = []
        # Token is derived from (expiry, strike, type) directly - NOT list position/index.
        # The strike list is re-centered around each day's spot, so the same absolute
        # strike can land at a different index on different days; hashing the index (an
        # earlier version of this fix did exactly that) reintroduces the same instability
        # this whole function exists to avoid.
        token_base = abs(hash(expiry)) % 10_000_000
        for strike in strikes:
            for opt_type in ("CE", "PE"):
                token = f"{token_base}{int(strike)}{'0' if opt_type == 'CE' else '1'}"
                instruments.append(
                    Instrument(
                        token=token, symbol=f"{self.underlying}{expiry}{int(strike)}{opt_type}", name=self.underlying,
                        expiry=expiry, strike=float(strike), lot_size=self.lot_size, instrument_type="OPTIDX",
                        exchange="NFO",
                    )
                )
        return instruments

    def _record_equity_point(self, bar: DailyBar) -> None:
        daily_state = journal.get_or_create_daily_state(bar.trade_date)
        open_position = journal.get_open_position()
        unrealized = self.strategy.position_pnl_rs(open_position) if open_position is not None else 0.0
        realized_total = self._total_realized_pnl()
        journal.append_equity_point(
            realized_pnl_rs=realized_total,
            unrealized_pnl_rs=unrealized,
            total_equity_rs=self.starting_capital_rs + realized_total + unrealized,
        )

    def _total_realized_pnl(self) -> float:
        return sum(t["realized_pnl_rs"] or 0.0 for t in journal.list_recent_positions(limit=10000))

    def _force_close_if_open(self, last_bar: DailyBar | None) -> None:
        if last_bar is None:
            return
        open_position = journal.get_open_position()
        if open_position is None:
            return
        intent = self.strategy.manual_exit()
        if intent is not None:
            self.oms.execute_exit(intent, self.daily_loss_limit_rs, self.max_consecutive_losses, trade_date=last_bar.trade_date)
