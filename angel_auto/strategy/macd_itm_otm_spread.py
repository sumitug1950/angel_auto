"""The flagship strategy: manually-directed (Long/Short), MACD-confirmed entry into an
ITM/OTM vertical spread on Nifty options. See the project plan for the full rationale -
this module wires together every piece built so far:

  direction request (manual) -> MACD gate (analytics.indicators) -> structure selection
  (the Buying/Selling button, unless an India VIX move past vix_override.threshold_pct forces
  one) -> the expiry you picked on the dashboard (one of the next `expiry_choices` expiries,
  used by both buttons) -> strikes from that structure's config block (buying/selling: strike
  grid, ITM/OTM delta targets - each strike's delta solved from its own live IV,
  data.market_data) -> fixed-Rs SL / trail-to-lock-in target /
  opposite-MACD exit / manual exit / mandatory square-off.

Every tunable lives in config/strategies.yaml (see settings.StrategyConfig).

Signal-only (Phase 4): every entry/exit decision comes back as an EntryIntent/ExitIntent.
The caller (OMS, Phase 6) is responsible for actually placing orders and confirming fills
(add_leg/update_position_status(OPEN) etc. happen once the OMS knows legs are filled, not
here) - this module only ever reads the persisted position state, never assumes a fill.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from angel_auto.analytics.indicators import compute_macd, current_state
from angel_auto.analytics.iv_rank import compute_iv_rank
from angel_auto.core.enums import LevelOrderStatus
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
from angel_auto.risk import pretrade as pretrade_checks
from angel_auto.settings import LegRulesConfig, StrategyConfig
from angel_auto.strategy.base import EntryIntent, ExitIntent, LegIntent, Strategy

log = get_logger(__name__)

MACD_STATE_TO_DIRECTION = {"BULLISH": Direction.LONG, "BEARISH": Direction.SHORT}
BUTTON_TO_STRUCTURE = {"BUYING": StructureType.DEBIT, "SELLING": StructureType.CREDIT}


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
        get_today=date.today,  # Callable[[], date] - injectable so backtest can drive its own clock
        get_now=None,  # Callable[[], datetime] (UTC) - injectable for tests; level-order retry timing
    ) -> None:
        self.config = config
        self.underlying = underlying
        self.lot_size = lot_size
        self.max_trades_per_day = max_trades_per_day
        self.instruments = instruments
        self.bars = bar_aggregator
        self.option_chain = option_chain
        self.get_current_vix = get_current_vix
        self.get_today = get_today
        self.get_now = get_now or (lambda: datetime.now(timezone.utc))
        # Latest entry problem to surface on the dashboard (e.g. MACD confirmed but no live
        # option quotes yet, or the OMS couldn't fill) - None when there's nothing to report.
        self.entry_notice: dict | None = None
        # Backtest only: nobody clicks an expiry button there, so the engine sets the expiry
        # here directly instead of overwriting the pick you saved on the dashboard.
        self.expiry_override: str | None = None

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

    def _macd_ready(self, macd_df) -> bool:
        return len(macd_df) >= self._macd_candles_needed()

    def _macd_candles_needed(self) -> int:
        return max(1, self.config.macd.min_candles_before_entry)

    # --- Entry: manual direction request + MACD gate ---------------------

    def on_direction_request(self, direction: Direction) -> EntryIntent | None:
        self.entry_notice = None  # a fresh click starts a fresh attempt
        macd_df = self._macd_df()
        state = current_state(macd_df) if self._macd_ready(macd_df) else None

        existing = journal.get_pending_direction_request()
        if existing is not None:
            journal.resolve_direction_request(existing["id"], PendingRequestStatus.REPLACED)
            log.info("direction_request_replaced", old_id=existing["id"], new_direction=direction.value)

        request_id = journal.create_direction_request(direction, macd_state_at_request=state or "UNKNOWN")

        if state is not None and self._direction_matches_state(direction, state):
            log.info("direction_request_immediate_execute", direction=direction.value, macd_state=state)
            return self._try_execute_entry(direction, request_id)

        log.info("direction_request_pending", direction=direction.value, macd_state=state)
        if not self._macd_ready(macd_df):
            self._set_notice(
                f"MACD abhi taiyaar ho raha hai ({len(macd_df)}/{self._macd_candles_needed()} candles) - "
                f"{self._request_label(direction, self.structure_preference())} Pending hai, taiyaar hote hi check hogi.",
                request_id,
                kind="warmup",
            )
        return None

    def cancel_pending_request(self) -> bool:
        pending = journal.get_pending_direction_request()
        if pending is None:
            return False
        journal.resolve_direction_request(pending["id"], PendingRequestStatus.CANCELLED)
        self.entry_notice = None
        log.info("direction_request_cancelled", request_id=pending["id"])
        return True

    def record_entry_notice(self, message: str) -> None:
        """For entry problems found after this strategy handed off an EntryIntent (e.g. the
        OMS couldn't fill the orders) - shown on the dashboard like the strategy's own."""
        self._set_notice(message, kind="order_failed")

    def _set_notice(self, message: str, request_id: int | None = None, kind: str = "info") -> None:
        notice = self.entry_notice
        if notice and notice["request_id"] == request_id and notice["message"] == message:
            return  # unchanged - keep the time it was first raised
        self.entry_notice = {"message": message, "kind": kind, "request_id": request_id, "at": datetime.now(timezone.utc)}

    def _notice_is(self, kind: str, request_id: int) -> bool:
        notice = self.entry_notice or {}
        return notice.get("kind") == kind and notice.get("request_id") == request_id

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

        # MACD *state*, not a same-candle crossover event: a request is only Pending because
        # MACD disagreed when it was clicked, so MACD agreeing now means the crossover has
        # happened. A state check also means neither a loop cycle that skips the exact
        # crossover candle nor an entry attempt that couldn't complete (no live quotes yet -
        # see _try_execute_entry) loses the confirmation: it retries every cycle for as long
        # as MACD still agrees, and goes back to waiting if MACD flips away again.
        if not self._macd_ready(macd_df):
            return None  # still warming up - the warm-up notice stays on the dashboard
        if self._notice_is("warmup", pending["id"]):
            self.entry_notice = None
        state = current_state(macd_df)
        if self._direction_matches_state(pending["direction"], state):
            retrying = self._notice_is("waiting_quotes", pending["id"]) or self._notice_is("no_expiry", pending["id"])
            if not retrying:  # don't re-log every retry
                log.info("pending_request_confirmed_by_macd", direction=pending["direction"].value, macd_state=state)
            return self._try_execute_entry(pending["direction"], pending["id"])
        return None

    # --- Entry construction ------------------------------------------------

    def _try_execute_entry(self, direction: Direction, direction_request_id: int) -> EntryIntent | None:
        pretrade = pretrade_checks.run_pretrade_checks(self.max_trades_per_day, trade_date=self.get_today())
        if not pretrade.allowed:
            log.warning("entry_blocked_pretrade_check", reason=pretrade.reason)
            journal.resolve_direction_request(direction_request_id, PendingRequestStatus.CANCELLED)
            self._set_notice(
                f"Trade nahi laga: {pretrade.reason}. {self._request_label(direction, self.structure_preference())} cancel ho gayi.",
                direction_request_id,
                kind="blocked",
            )
            return None

        iv_rank = self._compute_iv_rank()  # recorded for reference only - no longer drives structure selection
        structure_type = self._select_structure()
        expiry = self.selected_expiry()
        if expiry is None:
            # Stays Pending: picking an expiry on the dashboard lets the next cycle go ahead.
            if not self._notice_is("no_expiry", direction_request_id):
                log.warning("entry_waiting_for_expiry_pick", direction=direction.value)
            self._set_notice(
                f"Expiry nahi chuni (ya chuni hui expiry nikal gayi) - upar se expiry chuno. "
                f"{self._request_label(direction, structure_type)} Pending hai.",
                direction_request_id,
                kind="no_expiry",
            )
            return None

        legs = self._select_legs(direction, structure_type, expiry)
        if legs is None:
            # Not a reason to drop the click: quotes/Greeks can simply not be in yet (app just
            # restarted, feed blip, illiquid strike). The request stays Pending and
            # on_market_data retries every cycle while MACD still agrees.
            if not self._notice_is("waiting_quotes", direction_request_id):
                log.warning("entry_waiting_for_live_quotes", direction=direction.value, structure=structure_type.value, expiry=expiry)
            self._set_notice(
                f"MACD ne confirm kiya, par {expiry} ke option prices abhi nahi mile - "
                f"{self._request_label(direction, structure_type)} Pending hai, "
                f"har {self.config.check_interval_sec:g} sec dobara koshish ho rahi hai.",
                direction_request_id,
                kind="waiting_quotes",
            )
            return None

        self.entry_notice = None
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
            backup_sl_loss_rs=self._backup_sl_loss_rs(),
        )

    def _backup_sl_loss_rs(self) -> float | None:
        exit_cfg = self.config.exit
        return exit_cfg.sl_amount_rs * exit_cfg.broker_backup_sl_multiple if exit_cfg.broker_backup_sl else None

    def _compute_iv_rank(self) -> float | None:
        try:
            current_vix = self.get_current_vix()
            history = journal.get_vix_history(self.config.iv_rank_lookback_days, as_of=self.get_today())
            return compute_iv_rank(current_vix, history)
        except Exception:  # not enough VIX history yet, or feed unavailable - fall back to DEBIT default
            return None

    def _vix_spike_direction(self) -> str | None:
        """"UP", "DOWN", or None (no spike either way) vs yesterday's VIX close. A rise
        means rising fear/uncertainty - CREDIT (short ITM + hedge) is riskier that day. A
        drop means a calming market - good conditions for CREDIT, bad reason to force a
        cheap DEBIT buy. Both directions use the same threshold, symmetric either way."""
        try:
            current_vix = self.get_current_vix()
            previous_close = journal.get_previous_vix_close(before_date=self.get_today())
            if not previous_close:
                return None
            pct_change = (current_vix - previous_close) / previous_close * 100.0
            threshold = self.config.vix_override.threshold_pct
            if pct_change >= threshold:
                return "UP"
            if pct_change <= -threshold:
                return "DOWN"
            return None
        except Exception:
            return None

    def on_structure_request(self, structure_type: StructureType) -> None:
        """Manual Buying (DEBIT) / Selling (CREDIT) button - not a trigger and not MACD-
        gated, just updates the preference applied at the next entry. See _select_structure
        for how the VIX-spike override can still take precedence over this at that moment."""
        journal.set_structure_preference(structure_type)
        log.info("structure_preference_set", structure=structure_type.value)

    def structure_preference(self) -> StructureType:
        """The Buying/Selling button currently in effect - config's start_with until a button
        has ever been pressed."""
        return journal.get_structure_preference(default=BUTTON_TO_STRUCTURE[self.config.start_with])

    def _select_structure(self) -> StructureType:
        spike = self._vix_spike_direction()
        overrides = self.config.vix_override
        forced = {"UP": overrides.on_rise, "DOWN": overrides.on_fall}.get(spike, "OFF")
        if forced != "OFF":
            return BUTTON_TO_STRUCTURE[forced]
        return self.structure_preference()  # no configured override applies - respect your button

    def leg_rules(self, structure_type: StructureType) -> LegRulesConfig:
        return self.config.buying if structure_type == StructureType.DEBIT else self.config.selling

    def expiry_choices(self) -> list[dict]:
        """The next `expiry_choices` expiries (today's included) offered on the dashboard -
        [{"expiry", "days_left", "monthly"}], nearest first."""
        today = self.get_today()
        monthly = set(self.instruments.monthly_expiries(self.underlying))
        choices = []
        for expiry in self.instruments.available_expiries(self.underlying):
            expiry_date = datetime.strptime(expiry, "%d%b%Y").date()
            if expiry_date < today:
                continue
            choices.append({"expiry": expiry, "days_left": (expiry_date - today).days, "monthly": expiry in monthly})
            if len(choices) == self.config.expiry_choices:
                break
        return choices

    def selected_expiry(self) -> str | None:
        """The expiry picked on the dashboard - None if none is picked yet or it's no longer on
        offer (e.g. it has expired since)."""
        if self.expiry_override is not None:
            return self.expiry_override
        picked = journal.get_expiry_preference()
        return picked if picked in {choice["expiry"] for choice in self.expiry_choices()} else None

    def on_expiry_request(self, expiry: str) -> bool:
        """Dashboard expiry button. False (nothing changes) if `expiry` isn't currently on offer."""
        if expiry not in {choice["expiry"] for choice in self.expiry_choices()}:
            return False
        journal.set_expiry_preference(expiry)
        log.info("expiry_preference_set", expiry=expiry)
        return True

    @classmethod
    def _request_label(cls, direction: Direction, structure_type: StructureType) -> str:
        """Dashboard wording for a request - "CALL khareedne ki request" / "PUT bechne ki
        request" (the user reads Call/Put, not Long/Short)."""
        option = "CALL" if cls._option_type_for(direction, structure_type) == OptionType.CE else "PUT"
        action = "khareedne" if structure_type == StructureType.DEBIT else "bechne"
        return f"{option} {action} ki request"

    @staticmethod
    def _option_type_for(direction: Direction, structure_type: StructureType) -> OptionType:
        if structure_type == StructureType.DEBIT:
            return OptionType.CE if direction == Direction.LONG else OptionType.PE
        return OptionType.PE if direction == Direction.LONG else OptionType.CE

    def _select_legs(
        self, direction: Direction, structure_type: StructureType, expiry: str
    ) -> list[LegIntent] | None:
        option_type = self._option_type_for(direction, structure_type)
        rules = self.leg_rules(structure_type)
        grid_strikes = set(self.instruments.strikes_on_grid(self.underlying, expiry, rules.strike_grid))

        candidates = [
            q
            for q in self.option_chain.quotes_for_type_and_expiry(option_type.value, expiry)
            if q.strike in grid_strikes and q.delta is not None
        ]
        if not candidates:
            return None

        itm_quote = min(candidates, key=lambda q: abs(abs(q.delta) - rules.itm_delta))
        otm_quote = min(candidates, key=lambda q: abs(abs(q.delta) - rules.otm_delta))
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
                token=itm_quote.token, trading_symbol=itm_quote.trading_symbol, quantity=quantity,
                delta_at_selection=itm_quote.delta,
            ),
            LegIntent(
                option_type=option_type, strike=otm_quote.strike, role="OTM", side=otm_side,
                token=otm_quote.token, trading_symbol=otm_quote.trading_symbol, quantity=quantity,
                delta_at_selection=otm_quote.delta,
            ),
        ]

    # --- Exit evaluation -------------------------------------------------

    def position_pnl_rs(self, open_position: dict) -> float:
        """Live unrealized P&L for the given open position dict (as returned by
        journal.get_open_position()). Public - the dashboard's /api/status uses this too."""
        total = 0.0
        for leg in open_position["legs"]:
            if leg["entry_price"] is None:
                continue
            price = leg.get("exit_price")  # a leg already closed (e.g. by the broker backup SL) is locked in
            if price is None:
                quote = self.option_chain.get(leg["token"])
                if quote is None or quote.ltp <= 0:
                    continue
                price = quote.ltp
            if leg["side"] == OrderSide.BUY:
                total += (price - leg["entry_price"]) * leg["quantity"]
            else:
                total += (leg["entry_price"] - price) * leg["quantity"]
        return total

    def live_pnl_rs(self, open_position: dict) -> float | None:
        """Like position_pnl_rs, but None unless every still-open leg has a live quote. Exit
        rules must never judge a P&L that silently counts a leg as flat - e.g. right after a
        restart, before the feed has delivered that leg's first tick, a persisted trailing
        stop would otherwise fire on a phantom ₹0."""
        for leg in open_position["legs"]:
            if leg["entry_price"] is None or leg.get("exit_price") is not None:
                continue
            quote = self.option_chain.get(leg["token"])
            if quote is None or quote.ltp <= 0:
                return None
        return self.position_pnl_rs(open_position)

    def _check_exit(self, open_position: dict, macd_df) -> ExitIntent | None:
        pnl = self.live_pnl_rs(open_position)
        if pnl is None:
            return None
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

    # --- Nifty level orders + Nifty-price SL/target -------------------------------------
    #
    # A level order (set on the dashboard chart) enters its saved trade the moment Nifty spot
    # reaches the level - no MACD gate - and carries an optional Nifty-price SL/target onto
    # the position. Those sit on top of the Rs SL/target/trailing: whichever is hit first
    # exits. The app calls on_spot_price every level_order.check_interval_sec.

    @classmethod
    def _trade_name(cls, direction: Direction, structure_type: StructureType) -> str:
        """"CALL khareedo" / "PUT becho" - the dashboard's wording for a trade."""
        option = "CALL" if cls._option_type_for(direction, structure_type) == OptionType.CE else "PUT"
        return f"{option} {'khareedo' if structure_type == StructureType.DEBIT else 'becho'}"

    def place_level_order(
        self,
        direction: Direction,
        structure_type: StructureType,
        trigger_price: float,
        spot: float,
        spot_sl: float | None = None,
        spot_target: float | None = None,
    ) -> int:
        """Dashboard level order on the picked expiry - replaces any active one. Raises
        ValueError carrying the dashboard message when it can't be placed as given."""
        cfg = self.config.level_order
        if not cfg.enabled:
            raise ValueError("Level order band hai (strategies.yaml mein level_order.enabled: false)")
        if structure_type not in (StructureType.DEBIT, StructureType.CREDIT):
            raise ValueError("Buying ya Selling chuno")
        expiry = self.selected_expiry()
        if expiry is None:
            raise ValueError("Pehle Trade control mein expiry chuno")
        if journal.get_open_position() is not None:
            raise ValueError("Position pehle se khuli hai - level order uske band hone ke baad lagao")
        pretrade = pretrade_checks.run_pretrade_checks(self.max_trades_per_day, trade_date=self.get_today())
        if not pretrade.allowed:
            raise ValueError(f"Aaj naya trade nahi lag sakta: {pretrade.reason}")
        trigger_when = self._validated_trigger_when(direction, trigger_price, spot, spot_sl, spot_target)

        order_id = journal.create_level_order(
            self.get_today(), direction, structure_type, expiry, trigger_price, trigger_when, spot_sl, spot_target
        )
        log.info(
            "level_order_placed",
            level_order_id=order_id,
            trade=self._trade_name(direction, structure_type),
            expiry=expiry,
            level=trigger_price,
            trigger_when=trigger_when,
            spot=spot,
            spot_sl=spot_sl,
            spot_target=spot_target,
        )
        return order_id

    def modify_level_order(
        self, trigger_price: float, spot: float, spot_sl: float | None = None, spot_target: float | None = None
    ) -> None:
        """Moves the waiting level order's level / Nifty SL / target (lines dragged on the chart)
        - same trade, same expiry. Raises ValueError carrying the dashboard message."""
        order = journal.get_active_level_order()
        if order is None:
            raise ValueError("Koi level order nahi hai")
        if order["status"] != LevelOrderStatus.WAITING:
            raise ValueError("Level chhu chuka hai - order ja raha hai, ab badal nahi sakte")
        trigger_when = self._validated_trigger_when(order["direction"], trigger_price, spot, spot_sl, spot_target)
        if not journal.update_level_order(order["id"], trigger_price, trigger_when, spot_sl, spot_target):
            raise ValueError("Level chhu chuka hai - order ja raha hai, ab badal nahi sakte")
        log.info(
            "level_order_modified",
            level_order_id=order["id"],
            level=trigger_price,
            trigger_when=trigger_when,
            spot=spot,
            spot_sl=spot_sl,
            spot_target=spot_target,
        )

    def _validated_trigger_when(
        self, direction: Direction, trigger_price: float, spot: float, spot_sl: float | None, spot_target: float | None
    ) -> str:
        """Checks a level (and its Nifty SL/target) against live spot; returns which way spot
        must move to reach it."""
        gap = self.config.level_order.min_gap_points
        if spot <= 0:
            raise ValueError("Nifty ka live bhaav abhi nahi aaya - thodi der baad lagao")
        if abs(trigger_price - spot) < max(gap, 0.05):
            raise ValueError(
                f"Level Nifty ke abhi ke bhaav ({spot:,.2f}) ke bahut paas hai - kam se kam {gap:g} "
                "point door rakho, ya seedha Trade control ka CALL/PUT button dabao"
            )
        self._validate_spot_levels(direction, trigger_price, "level", spot_sl, spot_target)
        return "RISES_TO" if trigger_price > spot else "FALLS_TO"

    def cancel_level_order(self) -> bool:
        order = journal.get_active_level_order()
        if order is None:
            return False
        journal.resolve_level_order(order["id"], LevelOrderStatus.CANCELLED, note="Aapne cancel kiya")
        log.info("level_order_cancelled", level_order_id=order["id"])
        return True

    def set_position_spot_levels(self, spot: float, spot_sl: float | None, spot_target: float | None) -> None:
        """Nifty-price SL/target on the open position - any position, a MACD trade too. None
        removes that level. Raises ValueError carrying the dashboard message."""
        position = journal.get_open_position()
        if position is None:
            raise ValueError("Koi khuli position nahi hai")
        if spot_sl is not None or spot_target is not None:
            if spot <= 0:
                raise ValueError("Nifty ka live bhaav abhi nahi aaya - thodi der baad lagao")
            self._validate_spot_levels(position["direction"], spot, "Nifty ke abhi ke bhaav", spot_sl, spot_target)
        journal.set_position_spot_levels(position["id"], spot_sl, spot_target)
        log.info("position_spot_levels_set", position_id=position["id"], spot=spot, spot_sl=spot_sl, spot_target=spot_target)

    def _validate_spot_levels(
        self, direction: Direction, reference: float, reference_name: str, spot_sl: float | None, spot_target: float | None
    ) -> None:
        """A market-upar trade (LONG) stops out below `reference` and books profit above it;
        a market-neeche trade (SHORT) the other way round - each at least min_gap_points away."""
        gap = max(self.config.level_order.min_gap_points, 0.05)
        bullish = direction == Direction.LONG
        loss_side, profit_side = ("neeche", "upar") if bullish else ("upar", "neeche")
        if spot_sl is not None and ((reference - spot_sl) if bullish else (spot_sl - reference)) < gap:
            raise ValueError(
                f"Nifty SL {reference_name} ({reference:,.2f}) se kam se kam {gap:g} point {loss_side} hona chahiye"
            )
        if spot_target is not None and ((spot_target - reference) if bullish else (reference - spot_target)) < gap:
            raise ValueError(
                f"Nifty target {reference_name} ({reference:,.2f}) se kam se kam {gap:g} point {profit_side} hona chahiye"
            )

    def expire_stale_level_order(self) -> None:
        """A level order is valid only on the day it was placed."""
        order = journal.get_active_level_order()
        if order is not None and order["trade_date"] != self.get_today():
            self._resolve_level(
                order, LevelOrderStatus.EXPIRED, "Pichhle din ka level order - Nifty wahan nahi pahuncha tha, band kar diya."
            )

    def on_spot_price(self, spot: float) -> EntryIntent | ExitIntent | None:
        """Live Nifty spot: a level order's trigger/entry, or the open position's Nifty SL/
        target. At most one intent."""
        if spot <= 0:
            return None
        self.expire_stale_level_order()
        position = journal.get_open_position()
        order = journal.get_active_level_order()
        if order is not None:
            intent = self._check_level_order(order, spot, position)
            if intent is not None:
                return intent
        return self._check_spot_exit(position, spot) if position is not None else None

    @staticmethod
    def _level_hit(order: dict, spot: float) -> bool:
        if order["trigger_when"] == "RISES_TO":
            return spot >= order["trigger_price"]
        return spot <= order["trigger_price"]

    def _check_level_order(self, order: dict, spot: float, position: dict | None) -> EntryIntent | None:
        if position is not None:
            # One position at a time - and firing later, once it closes, would enter at a
            # moment nobody chose.
            if order["status"] == LevelOrderStatus.TRIGGERED or self._level_hit(order, spot):
                self._resolve_level(
                    order,
                    LevelOrderStatus.CANCELLED,
                    f"Nifty level ({order['trigger_price']:,.2f}) par pahuncha, par ek position pehle se khuli thi - level order cancel.",
                )
            return None
        if order["status"] == LevelOrderStatus.WAITING:
            if not self._level_hit(order, spot):
                return None
            journal.mark_level_order_triggered(order["id"])
            log.info("level_order_triggered", level_order_id=order["id"], spot=spot, level=order["trigger_price"])
            order = journal.get_level_order(order["id"])
        return self._try_level_entry(order)

    def _try_level_entry(self, order: dict) -> EntryIntent | None:
        cfg = self.config.level_order
        name = self._trade_name(order["direction"], order["structure_type"])
        waited = (self.get_now() - order["triggered_at"]).total_seconds() if order["triggered_at"] else 0.0
        if waited > cfg.entry_retry_sec:
            self._resolve_level(
                order,
                LevelOrderStatus.FAILED,
                f"Nifty level chhua, par {cfg.entry_retry_sec:g} sec tak {name} ka order nahi lag paaya "
                f"({order['expiry']} ke option prices nahi mile) - level order cancel.",
            )
            return None
        pretrade = pretrade_checks.run_pretrade_checks(self.max_trades_per_day, trade_date=self.get_today())
        if not pretrade.allowed:
            self._resolve_level(
                order, LevelOrderStatus.FAILED, f"Nifty level chhua, par trade nahi laga: {pretrade.reason}. Level order cancel."
            )
            return None
        if order["expiry"] not in {choice["expiry"] for choice in self.expiry_choices()}:
            self._resolve_level(
                order, LevelOrderStatus.FAILED, f"Level order ki expiry {order['expiry']} ab chunne layak nahi - level order cancel."
            )
            return None

        legs = self._select_legs(order["direction"], order["structure_type"], order["expiry"])
        if legs is None:
            # Stays TRIGGERED: retried every check until the quotes arrive or entry_retry_sec runs out.
            if not self._notice_is("level_waiting_quotes", None):
                log.warning("level_entry_waiting_for_live_quotes", level_order_id=order["id"], expiry=order["expiry"])
            self._set_notice(
                f"Nifty level chhua - {name} ke liye {order['expiry']} ke option prices ka intezaar, koshish chalu hai.",
                kind="level_waiting_quotes",
            )
            return None

        if self._notice_is("level_waiting_quotes", None):
            self.entry_notice = None
        log.info("level_entry_intent_built", level_order_id=order["id"], trade=name, strikes=[leg.strike for leg in legs])
        return EntryIntent(
            direction=order["direction"],
            structure_type=order["structure_type"],
            expiry=order["expiry"],
            legs=legs,
            iv_rank=self._compute_iv_rank(),
            backup_sl_loss_rs=self._backup_sl_loss_rs(),
            spot_sl=order["spot_sl"],
            spot_target=order["spot_target"],
            level_order_id=order["id"],
        )

    def _check_spot_exit(self, position: dict, spot: float) -> ExitIntent | None:
        bullish = position["direction"] == Direction.LONG
        spot_sl, spot_target = position.get("spot_sl"), position.get("spot_target")
        if spot_sl is not None and (spot <= spot_sl if bullish else spot >= spot_sl):
            log.info("exit_spot_sl", position_id=position["id"], spot=spot, spot_sl=spot_sl)
            return ExitIntent(reason=ExitReason.SPOT_SL)
        if spot_target is not None and (spot >= spot_target if bullish else spot <= spot_target):
            log.info("exit_spot_target", position_id=position["id"], spot=spot, spot_target=spot_target)
            return ExitIntent(reason=ExitReason.SPOT_TARGET)
        return None

    def _resolve_level(self, order: dict, status: LevelOrderStatus, message: str) -> None:
        journal.resolve_level_order(order["id"], status, note=message)
        log.info("level_order_resolved", level_order_id=order["id"], status=status.value, note=message)
        self._set_notice(message, kind="level")
