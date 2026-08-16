"""The flagship strategy: manually-directed (Long/Short), MACD-confirmed entry into an
ITM/OTM vertical spread on Nifty options. See the project plan for the full rationale -
this module wires together every piece built so far:

  direction request (manual) -> MACD gate (analytics.indicators) -> structure selection
  (DEBIT default / CREDIT above an IV-Rank threshold, analytics.iv_rank on India VIX) ->
  expiry selection (structure-dependent: DEBIT=monthly w/ min gap, CREDIT=nearest,
  data.instruments) -> strike selection (delta-targeted on the 100-pt grid, using each
  strike's own solved IV/delta, data.market_data) -> fixed-Rs SL / trail-to-lock-in target /
  opposite-MACD exit / manual exit / mandatory square-off.

Signal-only (Phase 4): every entry/exit decision comes back as an EntryIntent/ExitIntent.
The caller (OMS, Phase 6) is responsible for actually placing orders and confirming fills
(add_leg/update_position_status(OPEN) etc. happen once the OMS knows legs are filled, not
here) - this module only ever reads the persisted position state, never assumes a fill.
"""
from __future__ import annotations

from angel_auto.analytics.indicators import compute_macd, current_state, detect_crossover
from angel_auto.analytics.iv_rank import compute_iv_rank
from angel_auto.core.enums import (
    Direction,
    ExitReason,
    OptionType,
    OrderSide,
    PendingRequestStatus,
    StructureType,
)
from angel_auto.data.instruments import InstrumentMaster
from angel_auto.data.market_data import BarAggregator, OptionChainSnapshot
from angel_auto.logging_conf import get_logger
from angel_auto.persistence import journal
from angel_auto.settings import StrategyConfig
from angel_auto.strategy.base import EntryIntent, ExitIntent, LegIntent, Strategy

log = get_logger(__name__)

MACD_STATE_TO_DIRECTION = {"BULLISH": Direction.LONG, "BEARISH": Direction.SHORT}


class MacdItmOtmSpreadStrategy(Strategy):
    def __init__(
        self,
        config: StrategyConfig,
        underlying: str,
        lot_size: int,
        max_trades_per_day: int,
        instruments: InstrumentMaster,
        bar_aggregator: BarAggregator,
        option_chain: OptionChainSnapshot,
        get_current_vix,  # Callable[[], float]
    ) -> None:
        self.config = config
        self.underlying = underlying
        self.lot_size = lot_size
        self.max_trades_per_day = max_trades_per_day
        self.instruments = instruments
        self.bars = bar_aggregator
        self.option_chain = option_chain
        self.get_current_vix = get_current_vix

    # --- MACD helpers ---------------------------------------------------

    def _macd_df(self):
        closes = self.bars.closes_series()
        return compute_macd(
            closes,
            fast_period=self.config.macd.fast_period,
            slow_period=self.config.macd.slow_period,
            signal_period=self.config.macd.signal_period,
        )

    @staticmethod
    def _direction_matches_state(direction: Direction, macd_state: str) -> bool:
        return MACD_STATE_TO_DIRECTION.get(macd_state) == direction

    # --- Entry: manual direction request + MACD gate ---------------------

    def on_direction_request(self, direction: Direction) -> EntryIntent | None:
        macd_df = self._macd_df()
        state = current_state(macd_df) if len(macd_df) >= 1 else None

        existing = journal.get_pending_direction_request()
        if existing is not None:
            journal.resolve_direction_request(existing["id"], PendingRequestStatus.REPLACED)
            log.info("direction_request_replaced", old_id=existing["id"], new_direction=direction.value)

        request_id = journal.create_direction_request(direction, macd_state_at_request=state or "UNKNOWN")

        if state is not None and self._direction_matches_state(direction, state):
            log.info("direction_request_immediate_execute", direction=direction.value, macd_state=state)
            return self._try_execute_entry(direction, request_id)

        log.info("direction_request_pending", direction=direction.value, macd_state=state)
        return None

    def cancel_pending_request(self) -> bool:
        pending = journal.get_pending_direction_request()
        if pending is None:
            return False
        journal.resolve_direction_request(pending["id"], PendingRequestStatus.CANCELLED)
        log.info("direction_request_cancelled", request_id=pending["id"])
        return True

    # --- Per-candle evaluation --------------------------------------------

    def on_market_data(self) -> EntryIntent | ExitIntent | None:
        macd_df = self._macd_df()
        if len(macd_df) < 2:
            return None

        open_position = journal.get_open_position()
        if open_position is not None:
            return self._check_exit(open_position, macd_df)

        pending = journal.get_pending_direction_request()
        if pending is None:
            return None

        crossover = detect_crossover(macd_df)
        if crossover == "NONE":
            return None
        if self._direction_matches_state(pending["direction"], crossover):
            log.info("pending_request_confirmed_by_crossover", direction=pending["direction"].value, crossover=crossover)
            return self._try_execute_entry(pending["direction"], pending["id"])
        return None

    # --- Entry construction ------------------------------------------------

    def _try_execute_entry(self, direction: Direction, direction_request_id: int) -> EntryIntent | None:
        daily_state = journal.get_or_create_daily_state()
        if daily_state["trading_halted"]:
            log.warning("entry_blocked_trading_halted", reason=daily_state["halt_reason"])
            journal.resolve_direction_request(direction_request_id, PendingRequestStatus.CANCELLED)
            return None
        if daily_state["trades_taken"] >= self.max_trades_per_day:
            log.warning("entry_blocked_max_trades_per_day", trades_taken=daily_state["trades_taken"])
            journal.resolve_direction_request(direction_request_id, PendingRequestStatus.CANCELLED)
            return None

        iv_rank = self._compute_iv_rank()
        structure_type = self._select_structure(iv_rank)
        expiry = self._select_expiry(structure_type)

        legs = self._select_legs(direction, structure_type, expiry)
        if legs is None:
            log.warning("entry_aborted_no_live_quotes", direction=direction.value, structure=structure_type.value)
            journal.resolve_direction_request(direction_request_id, PendingRequestStatus.CANCELLED)
            return None

        journal.resolve_direction_request(direction_request_id, PendingRequestStatus.EXECUTED)
        log.info(
            "entry_intent_built",
            direction=direction.value,
            structure=structure_type.value,
            expiry=expiry,
            iv_rank=iv_rank,
            strikes=[leg.strike for leg in legs],
        )
        return EntryIntent(
            direction=direction,
            structure_type=structure_type,
            expiry=expiry,
            legs=legs,
            iv_rank=iv_rank,
            direction_request_id=direction_request_id,
        )

    def _compute_iv_rank(self) -> float | None:
        try:
            current_vix = self.get_current_vix()
            history = journal.get_vix_history(self.config.structure.iv_rank_lookback_days)
            return compute_iv_rank(current_vix, history)
        except Exception:  # not enough VIX history yet, or feed unavailable - fall back to DEBIT default
            return None

    def _select_structure(self, iv_rank: float | None) -> StructureType:
        if iv_rank is not None and iv_rank >= self.config.structure.credit_iv_rank_threshold:
            return StructureType.CREDIT
        return StructureType.DEBIT

    def _select_expiry(self, structure_type: StructureType) -> str:
        if structure_type == StructureType.DEBIT:
            return self.instruments.select_monthly_expiry(self.underlying, self.config.expiry.debit_min_days_gap)
        return self.instruments.nearest_weekly_expiry(self.underlying)

    @staticmethod
    def _option_type_for(direction: Direction, structure_type: StructureType) -> OptionType:
        if structure_type == StructureType.DEBIT:
            return OptionType.CE if direction == Direction.LONG else OptionType.PE
        return OptionType.PE if direction == Direction.LONG else OptionType.CE

    def _select_legs(
        self, direction: Direction, structure_type: StructureType, expiry: str
    ) -> list[LegIntent] | None:
        option_type = self._option_type_for(direction, structure_type)
        grid_strikes = set(self.instruments.strikes_on_grid(self.underlying, expiry, self.config.strikes.strike_grid))

        candidates = [
            q
            for q in self.option_chain.quotes_for_type(option_type.value)
            if q.strike in grid_strikes and q.delta is not None
        ]
        if not candidates:
            return None

        itm_quote = min(candidates, key=lambda q: abs(abs(q.delta) - self.config.strikes.itm_delta_target))
        otm_quote = min(candidates, key=lambda q: abs(abs(q.delta) - self.config.strikes.otm_delta_target))
        if itm_quote.token == otm_quote.token:
            return None  # not enough distinct strikes with live Greeks yet

        if structure_type == StructureType.DEBIT:
            itm_side, otm_side = OrderSide.BUY, OrderSide.SELL
        else:
            itm_side, otm_side = OrderSide.SELL, OrderSide.BUY

        quantity = self.lot_size * self.config.sizing.lots

        return [
            LegIntent(
                option_type=option_type, strike=itm_quote.strike, role="ITM", side=itm_side,
                token=itm_quote.token, trading_symbol=itm_quote.trading_symbol, delta_at_selection=itm_quote.delta,
            ),
            LegIntent(
                option_type=option_type, strike=otm_quote.strike, role="OTM", side=otm_side,
                token=otm_quote.token, trading_symbol=otm_quote.trading_symbol, delta_at_selection=otm_quote.delta,
            ),
        ]

    # --- Exit evaluation -------------------------------------------------

    def _position_pnl_rs(self, open_position: dict) -> float:
        total = 0.0
        for leg in open_position["legs"]:
            quote = self.option_chain.get(leg["token"])
            if quote is None or leg["entry_price"] is None or quote.ltp <= 0:
                continue
            if leg["side"] == OrderSide.BUY:
                total += (quote.ltp - leg["entry_price"]) * leg["quantity"]
            else:
                total += (leg["entry_price"] - quote.ltp) * leg["quantity"]
        return total

    def _check_exit(self, open_position: dict, macd_df) -> ExitIntent | None:
        pnl = self._position_pnl_rs(open_position)
        exit_cfg = self.config.exit

        if pnl <= -exit_cfg.sl_amount_rs:
            log.info("exit_fixed_sl", position_id=open_position["id"], pnl=pnl)
            return ExitIntent(reason=ExitReason.FIXED_SL)

        target = exit_cfg.target_amount_rs
        trail_active = open_position["trail_active"] or pnl >= target
        if trail_active:
            peak = max(open_position["peak_profit_rs"], pnl)
            if peak != open_position["peak_profit_rs"] or not open_position["trail_active"]:
                journal.update_trailing_peak(open_position["id"], peak, trail_active=True)
            trailing_stop = peak - exit_cfg.trail_gap_rs
            if pnl <= trailing_stop:
                log.info("exit_trailing_stop", position_id=open_position["id"], pnl=pnl, peak=peak)
                return ExitIntent(reason=ExitReason.TRAILING_STOP)
            return None  # trailing but not stopped out yet

        if exit_cfg.exit_on_opposite_macd:
            # current *state*, not just a same-candle crossover event - a state check is
            # robust to a restart/gap missing the exact transition candle, whereas requiring
            # a fresh crossover on every single call could miss the exit entirely if the
            # MACD had already flipped before we next got to look.
            state = current_state(macd_df)
            direction = open_position["direction"]
            opposite = (direction == Direction.LONG and state == "BEARISH") or (
                direction == Direction.SHORT and state == "BULLISH"
            )
            if opposite:
                log.info("exit_opposite_macd", position_id=open_position["id"], macd_state=state)
                return ExitIntent(reason=ExitReason.OPPOSITE_MACD)

        return None

    def manual_exit(self) -> ExitIntent | None:
        if journal.get_open_position() is None:
            return None
        log.info("exit_manual")
        return ExitIntent(reason=ExitReason.MANUAL_EXIT)

    def on_square_off_trigger(self) -> ExitIntent | None:
        if journal.get_open_position() is None:
            return None
        log.info("exit_square_off")
        return ExitIntent(reason=ExitReason.SQUARE_OFF)
