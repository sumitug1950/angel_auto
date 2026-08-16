"""Automatic, fully hands-off single-leg strategy - no manual Long/Short/Buying-Selling
buttons, driven entirely by a tick-by-tick MACD/Signal crossover classified by which side
of the zero line it occurs on (see analytics/tick_macd.py for the exact rule).

Two config-differentiated instances of this SAME class run concurrently (see
strategy/registry.py + config/strategies.yaml's zero_cross_strategies list):
  - "atm_sell_macd_zero": single_leg.side=SELL, itm_offset_count=0 (ATM) - always shorts
    the ATM option, PUT on a bullish signal / CALL on a bearish signal.
  - "itm4_buy_macd_zero": single_leg.side=BUY, itm_offset_count=4 - always buys the option
    4 strikes in-the-money, CALL on a bullish signal / PUT on a bearish signal.

Each keeps its own entirely independent trade record (Position.strategy_name) so the two
can be compared directly - "do alag trades", not two legs of one spread.

Unlike the flagship (MacdItmOtmSpreadStrategy), this is tick-driven, not candle-driven:
on_market_data() is a no-op (nothing calls it - there's no 15-sec candle loop for this
strategy); the real entry point is on_tick_price(), called once per incoming spot tick.
"""
from __future__ import annotations

from datetime import date, datetime

from angel_auto.analytics.tick_macd import TickMacdEngine
from angel_auto.core.enums import Direction, ExitReason, OptionType, OrderSide, StructureType
from angel_auto.data.instruments import InstrumentMaster
from angel_auto.data.market_data import OptionChainSnapshot
from angel_auto.logging_conf import get_logger
from angel_auto.persistence import journal
from angel_auto.risk import pretrade as pretrade_checks
from angel_auto.scheduler.jobs import parse_hhmm
from angel_auto.settings import StrategyConfig
from angel_auto.strategy.base import EntryIntent, ExitIntent, LegIntent, Strategy

log = get_logger(__name__)


class MacdZeroCrossSingleLegStrategy(Strategy):
    def __init__(
        self,
        strategy_name: str,
        config: StrategyConfig,
        underlying: str,
        lot_size: int,
        instruments: InstrumentMaster,
        option_chain: OptionChainSnapshot,
        get_today=date.today,  # Callable[[], date] - injectable for future replay/backtest use
        get_now=datetime.now,  # Callable[[], datetime] - injectable so entry-window tests are deterministic
    ) -> None:
        if config.single_leg is None:
            raise ValueError(f"strategy '{strategy_name}' config is missing its required single_leg block")
        self.strategy_name = strategy_name
        self.config = config
        self.underlying = underlying
        self.lot_size = lot_size
        self.instruments = instruments
        self.option_chain = option_chain
        self.get_today = get_today
        self.get_now = get_now

        self.tick_macd = TickMacdEngine(
            fast_period=config.macd.fast_period,
            slow_period=config.macd.slow_period,
            signal_period=config.macd.signal_period,
        )
        self._entry_start = parse_hhmm(config.entry_window.start)
        self._entry_end = parse_hhmm(config.entry_window.end)
        self.role_label = "ATM" if config.single_leg.itm_offset_count == 0 else f"ITM{config.single_leg.itm_offset_count}"

    # --- Strategy ABC: this strategy has no manual triggers, everything is automatic ----

    def on_direction_request(self, direction: Direction) -> EntryIntent | None:
        return None

    def on_structure_request(self, structure_type) -> None:
        return None

    def cancel_pending_request(self) -> bool:
        return False

    def on_market_data(self) -> EntryIntent | ExitIntent | None:
        """No-op - this strategy is tick-driven (see on_tick_price), not candle-driven.
        Kept only to satisfy the Strategy ABC; nothing calls this for these instances."""
        return None

    # --- The real entry point: called once per incoming spot tick -----------------------

    def on_tick_price(self, spot_price: float) -> EntryIntent | ExitIntent | None:
        signal = self.tick_macd.update(spot_price)
        if not self.tick_macd.is_warmed_up:
            return None

        open_position = journal.get_open_position(strategy_name=self.strategy_name)
        if open_position is not None:
            return self._check_exit(open_position)

        if signal == "NONE":
            return None
        if not self._within_entry_window():
            return None
        return self._try_execute_entry(signal, spot_price)

    def _within_entry_window(self) -> bool:
        now_t = self.get_now().time()
        return self._entry_start <= now_t <= self._entry_end

    # --- Entry ----------------------------------------------------------------------

    def _option_type_for(self, signal: str) -> OptionType:
        side = self.config.single_leg.side
        if signal == "BULLISH":
            return OptionType.CE if side == "BUY" else OptionType.PE
        return OptionType.PE if side == "BUY" else OptionType.CE

    def _try_execute_entry(self, signal: str, spot_price: float) -> EntryIntent | None:
        pretrade = pretrade_checks.run_pretrade_checks(
            self.config.risk_override.max_trades_per_day,
            trade_date=self.get_today(),
            strategy_name=self.strategy_name,
        )
        if not pretrade.allowed:
            log.info("zero_cross_entry_blocked_pretrade_check", strategy=self.strategy_name, reason=pretrade.reason)
            return None

        expiry = self.instruments.nearest_weekly_expiry(self.underlying, as_of=self.get_today())
        option_type = self._option_type_for(signal)
        grid = self.config.single_leg.strike_grid
        offset = self.config.single_leg.itm_offset_count

        try:
            if offset == 0:
                strike = self.instruments.atm_strike(self.underlying, expiry, spot_price, grid=grid)
            else:
                strike = self.instruments.itm_offset_strike(
                    self.underlying, expiry, spot_price, option_type.value, offset, grid=grid
                )
        except LookupError:
            log.warning("zero_cross_entry_aborted_strike_lookup_failed", strategy=self.strategy_name)
            return None

        quote = next(
            (
                q
                for q in self.option_chain.quotes_for_type_and_expiry(option_type.value, expiry)
                if abs(q.strike - strike) < 0.01
            ),
            None,
        )
        if quote is None or quote.ltp <= 0:
            log.warning("zero_cross_entry_aborted_no_live_quote", strategy=self.strategy_name, strike=strike)
            return None

        quantity = self.lot_size * self.config.sizing.lots
        leg = LegIntent(
            option_type=option_type,
            strike=quote.strike,
            role=self.role_label,
            side=OrderSide(self.config.single_leg.side),
            token=quote.token,
            trading_symbol=quote.trading_symbol,
            quantity=quantity,
        )
        direction = Direction.LONG if signal == "BULLISH" else Direction.SHORT
        log.info(
            "zero_cross_entry_intent_built",
            strategy=self.strategy_name,
            signal=signal,
            option_type=option_type.value,
            strike=quote.strike,
            side=self.config.single_leg.side,
        )
        return EntryIntent(direction=direction, structure_type=StructureType.SINGLE_LEG, expiry=expiry, legs=[leg])

    # --- Exit -------------------------------------------------------------------------

    def position_pnl_rs(self, open_position: dict) -> float:
        """Live unrealized P&L for this strategy's single open leg. Public - the dashboard
        reads this too."""
        leg = open_position["legs"][0]
        quote = self.option_chain.get(leg["token"])
        if quote is None or leg["entry_price"] is None or quote.ltp <= 0:
            return 0.0
        if leg["side"] == OrderSide.BUY:
            return (quote.ltp - leg["entry_price"]) * leg["quantity"]
        return (leg["entry_price"] - quote.ltp) * leg["quantity"]

    def _check_exit(self, open_position: dict) -> ExitIntent | None:
        pnl = self.position_pnl_rs(open_position)
        if pnl <= -self.config.zero_cross_exit.sl_amount_rs:
            log.info("zero_cross_exit_fixed_sl", strategy=self.strategy_name, pnl=pnl)
            return ExitIntent(reason=ExitReason.FIXED_SL)

        # Current zero-line SIDE, not just a same-tick crossover event - robust to a
        # restart/missed tick, same reasoning as the flagship's exit_on_opposite_macd.
        current_side = self.tick_macd.current_side
        direction = open_position["direction"]
        opposite = (direction == Direction.LONG and current_side == "BEARISH") or (
            direction == Direction.SHORT and current_side == "BULLISH"
        )
        if opposite:
            log.info("zero_cross_exit_opposite_signal", strategy=self.strategy_name, current_side=current_side)
            return ExitIntent(reason=ExitReason.OPPOSITE_ZERO_CROSS)
        return None

    def manual_exit(self) -> ExitIntent | None:
        if journal.get_open_position(strategy_name=self.strategy_name) is None:
            return None
        log.info("zero_cross_exit_manual", strategy=self.strategy_name)
        return ExitIntent(reason=ExitReason.MANUAL_EXIT)

    def on_square_off_trigger(self) -> ExitIntent | None:
        if journal.get_open_position(strategy_name=self.strategy_name) is None:
            return None
        log.info("zero_cross_exit_square_off", strategy=self.strategy_name)
        return ExitIntent(reason=ExitReason.SQUARE_OFF)
