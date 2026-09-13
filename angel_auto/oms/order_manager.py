"""Order Manager - turns strategy intents (EntryIntent/ExitIntent) into orders via a
BrokerAdapter (paper / live / backtest - same interface), enforcing the leg-sequencing and
fill-failure rules:

  CREDIT structure: hedge (OTM buy) sent first, ITM short only after the hedge is confirmed
  filled - never briefly naked. Hedge fails -> abort, ITM short never sent. ITM short fails
  after the hedge filled -> the hedge is closed again.

  DEBIT structure: ITM buy first, then OTM sell (the worst case is a plain long option). OTM
  sell failing after ITM filled -> retry a few times, then leave the ITM leg standing alone
  (still covered by the strategy's SL/trailing).

  Exits: short legs are bought back before long legs are sold - a naked short needs full
  margin. A leg that can't be closed leaves the position OPEN; the next attempt only closes
  what is still open.

Fills are confirmed, never assumed: a live broker acknowledges an order as OPEN and reports
the fill later, so every order is polled (get_order_state) to a final state. One still
working at fill_timeout_sec is cancelled and read again - it may have filled before the
cancel landed, and that fill is real. A partial fill is never left behind: on entry the
filled part is closed again, on exit the remainder is re-sent. If an order's fate can't be
established at all, the OMS freezes (no further orders until restart) and halts trading
rather than guess and risk a duplicate order.

Every order carries its journal id as the broker ordertag ("AA<id>"), and
recover_after_restart() uses that plus the broker's order/position books to settle whatever
a crash or shutdown left half-done before trading resumes.

Order types: marketable LIMIT for entries (LTP +/- a buffer, on the 0.05 tick grid), MARKET
for exits. A CREDIT position's short leg also gets a broker-side STOPLOSS_LIMIT buy (see
EntryIntent.backup_sl_loss_rs) that protects it if this app stops. It is cancelled before
any app-driven exit; if it triggers, its fill is what closes that leg. DEBIT positions get
none: a stop on the long leg would leave the short leg naked.
"""
from __future__ import annotations

import math
import time
from collections.abc import Callable
from datetime import date

from angel_auto.analytics.charges import estimate_charges_rs
from angel_auto.broker.base import BrokerAdapter, MarginLeg, OrderRequest, OrderResult, OrderState
from angel_auto.core.enums import ExitReason, OrderSide, OrderStatus, PositionStatus, StructureType
from angel_auto.logging_conf import get_logger
from angel_auto.persistence import journal
from angel_auto.risk import circuit_breaker
from angel_auto.settings import ChargesConfig
from angel_auto.strategy.base import EntryIntent, ExitIntent, LegIntent

log = get_logger(__name__)

EXCHANGE = "NFO"
DEFAULT_STRATEGY_NAME = "macd_itm_otm_spread"
TICK = 0.05
FINAL_STATUSES = {OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED}
WORKING_STATUSES = {OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED}
SL_VARIETY = "STOPLOSS"


class OrderStuckError(Exception):
    """An order's final state couldn't be established - sending anything more could duplicate it."""


def to_tick(price: float, up: bool) -> float:
    steps = round(price / TICK, 6)
    return round((math.ceil(steps) if up else math.floor(steps)) * TICK, 2)


def order_tag(order_id: int) -> str:
    """The broker ordertag for a journal order - how a restart finds an order whose id was never saved."""
    return f"AA{order_id}"


def _opposite(side: OrderSide) -> OrderSide:
    return OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY


class OrderManager:
    def __init__(
        self,
        broker: BrokerAdapter,
        product_type: str = "INTRADAY",
        entry_slippage_buffer_pts: float = 1.0,
        max_otm_retry_attempts: int = 3,
        retry_delay_sec: float = 1.0,
        charges_config: ChargesConfig | None = None,
        fill_timeout_sec: float = 10.0,
        fill_poll_interval_sec: float = 2.0,
        exit_attempts: int = 3,
        check_margin: bool = False,
        place_backup_sl: bool = False,
        sl_limit_buffer_pts: float = 5.0,
        order_gate: Callable[[bool], str | None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.broker = broker
        self.product_type = product_type
        self.entry_slippage_buffer_pts = entry_slippage_buffer_pts
        self.max_otm_retry_attempts = max_otm_retry_attempts
        self.retry_delay_sec = retry_delay_sec
        self.charges_config = charges_config
        self.fill_timeout_sec = fill_timeout_sec
        self.fill_poll_interval_sec = fill_poll_interval_sec
        self.exit_attempts = exit_attempts
        self.check_margin = check_margin
        self.place_backup_sl = place_backup_sl
        self.sl_limit_buffer_pts = sl_limit_buffer_pts
        # order_gate(is_entry) -> reason no order may go out right now (market closed, dead
        # feed, ...) or None. Checked before any entry or exit sends anything.
        self.order_gate = order_gate
        self.sleep = sleep
        self.clock = clock

        # Set once an order's fate is unknown; blocks every further order until restart.
        self.frozen_reason: str | None = None
        # Human-readable outcome of the last execute_entry/execute_exit (dashboard notice), if any.
        self.last_notice: str | None = None
        self._leg_message = ""

    # --- Entry --------------------------------------------------------------

    def execute_entry(
        self, intent: EntryIntent, trade_date: date | None = None, strategy_name: str = DEFAULT_STRATEGY_NAME
    ) -> int | None:
        """Returns the new position's id on success, None if the entry was aborted (see
        last_notice for why). `trade_date` defaults to real today; the backtest engine passes
        its simulated day so the daily trade-count counter scopes to the day being replayed."""
        self.last_notice = None
        if self.frozen_reason:
            self.last_notice = self.frozen_reason
            return None
        if self._gated(is_entry=True):
            return None
        if self.check_margin and not self._margin_ok(intent):
            return None

        position_id = journal.create_position(
            intent.direction,
            intent.structure_type,
            intent.expiry,
            direction_request_id=intent.direction_request_id,
            iv_rank_at_entry=intent.iv_rank,
            strategy_name=strategy_name,
            spot_sl=intent.spot_sl,
            spot_target=intent.spot_target,
        )
        try:
            return self._enter_position(position_id, intent, trade_date, strategy_name)
        except OrderStuckError as exc:
            self._freeze(str(exc))  # position stays OPENING - visible, and settled on restart
            return None

    def _enter_position(self, position_id: int, intent: EntryIntent, trade_date: date | None, strategy_name: str) -> int | None:
        itm_leg = next(leg for leg in intent.legs if leg.role == "ITM")
        otm_leg = next(leg for leg in intent.legs if leg.role == "OTM")
        credit = intent.structure_type == StructureType.CREDIT
        first_leg, second_leg = (otm_leg, itm_leg) if credit else (itm_leg, otm_leg)  # hedge first / ITM first

        first_leg_id = self._persist_leg(position_id, first_leg)
        first_fill = self._enter_leg(first_leg_id, first_leg)
        if first_fill is None:
            log.error("entry_aborted_first_leg_failed", position_id=position_id, role=first_leg.role)
            journal.update_position_status(position_id, PositionStatus.ABORTED)
            self.last_notice = self.last_notice or (
                f"Order nahi laga: {first_leg.role} {first_leg.side.value} {first_leg.trading_symbol} - {self._leg_message}"
            )
            return None
        journal.update_leg_fill(first_leg_id, entry_price=first_fill)

        second_leg_id = self._persist_leg(position_id, second_leg)
        second_fill = self._enter_leg(second_leg_id, second_leg)

        if second_fill is None:
            if credit:
                log.warning("entry_second_leg_failed_unwinding_hedge", position_id=position_id)
                reason = self._leg_message
                closed, price = self._close_quantity(
                    first_leg_id, first_leg.trading_symbol, first_leg.token, first_leg.side, first_leg.quantity
                )
                if price is not None:
                    journal.update_leg_fill(first_leg_id, exit_price=price)
                journal.update_position_status(position_id, PositionStatus.ABORTED)
                if closed < first_leg.quantity:
                    self._freeze(
                        f"Hedge leg {first_leg.trading_symbol} wapas band nahi ho payi ({closed}/{first_leg.quantity} qty) - "
                        "Angel One app mein turant position check karein."
                    )
                    return None
                self.last_notice = f"Order nahi laga: ITM sell {second_leg.trading_symbol} - {reason}. Hedge wapas band kar diya."
                return None
            second_fill = self._retry_leg(second_leg_id, second_leg)
            if second_fill is None:
                log.warning("entry_otm_leg_unfilled_itm_stands_alone", position_id=position_id)
                self.last_notice = f"OTM sell {second_leg.trading_symbol} nahi laga ({self._leg_message}) - sirf ITM buy khula hai."

        if second_fill is not None:
            journal.update_leg_fill(second_leg_id, entry_price=second_fill)

        journal.update_position_status(position_id, PositionStatus.OPEN, set_entry_time=True)
        journal.increment_daily_trade_count(trade_date, strategy_name=strategy_name)
        log.info("entry_executed", position_id=position_id, strategy=strategy_name, structure=intent.structure_type.value)

        if self.place_backup_sl and credit and intent.backup_sl_loss_rs and second_fill is not None:
            self._place_backup_sl(second_leg_id, second_leg, second_fill, intent.backup_sl_loss_rs)
        return position_id

    def _persist_leg(self, position_id: int, leg: LegIntent) -> int:
        return journal.add_leg(
            position_id,
            leg.token,
            leg.trading_symbol,
            leg.option_type,
            leg.strike,
            leg.role,
            leg.side,
            leg.quantity,
            delta_at_selection=leg.delta_at_selection,
        )

    def _enter_leg(self, leg_id: int, leg: LegIntent) -> float | None:
        """Average fill price once the whole leg is filled; None otherwise (self._leg_message
        says why). A partially filled leg is closed again rather than kept as a fraction."""
        self._leg_message = ""
        ltp = self.broker.get_ltp(EXCHANGE, leg.trading_symbol, leg.token)
        if ltp <= 0:
            order_id = journal.add_order(leg_id, leg.side, "LIMIT", leg.quantity)
            journal.update_order_status(order_id, OrderStatus.REJECTED, reject_reason="no live quote")
            self._leg_message = "live price nahi mila"
            return None

        limit_price = self._marketable_limit_price(ltp, leg.side)
        order_id = journal.add_order(leg_id, leg.side, "LIMIT", leg.quantity, price=limit_price)
        result = self.broker.place_order(
            OrderRequest(
                exchange=EXCHANGE,
                trading_symbol=leg.trading_symbol,
                token=leg.token,
                side=leg.side,
                quantity=leg.quantity,
                order_type="LIMIT",
                product_type=self.product_type,
                price=limit_price,
                tag=order_tag(order_id),
            )
        )
        filled, average_price, message = self._confirm_fill(order_id, result, leg.quantity)
        if filled == leg.quantity:
            return average_price if average_price is not None else limit_price

        self._leg_message = message or f"{self.fill_timeout_sec:g} sec mein fill nahi hua"
        if filled > 0:
            log.warning("entry_leg_partial_fill_unwinding", leg_id=leg_id, filled=filled, quantity=leg.quantity)
            closed, _ = self._close_quantity(leg_id, leg.trading_symbol, leg.token, leg.side, filled)
            if closed < filled:
                raise OrderStuckError(
                    f"{leg.trading_symbol} ka adhura fill ({filled} qty) wapas band nahi ho paya - "
                    "Angel One app mein turant position check karein."
                )
        return None

    def _retry_leg(self, leg_id: int, leg: LegIntent) -> float | None:
        for attempt in range(1, self.max_otm_retry_attempts + 1):
            log.info("entry_leg_retry", leg_id=leg_id, attempt=attempt)
            self.sleep(self.retry_delay_sec)
            fill_price = self._enter_leg(leg_id, leg)
            if fill_price is not None:
                return fill_price
        return None

    def _marketable_limit_price(self, ltp: float, side: OrderSide) -> float:
        buffer = self.entry_slippage_buffer_pts
        return to_tick(ltp + buffer, up=True) if side == OrderSide.BUY else to_tick(max(ltp - buffer, TICK), up=False)

    def _margin_ok(self, intent: EntryIntent) -> bool:
        legs = [MarginLeg(EXCHANGE, leg.trading_symbol, leg.token, leg.side, leg.quantity, self.product_type) for leg in intent.legs]
        try:
            result = self.broker.check_margin(legs)
        except Exception as exc:  # noqa: BLE001 - no answer means no trade, never a guess
            log.exception("margin_check_failed")
            self.last_notice = f"Margin check nahi ho paya ({exc}) - trade nahi lagaya."
            return False
        if result.is_affordable:
            log.info("margin_check_ok", required_rs=result.required_margin_rs, available_rs=result.available_margin_rs)
            return True
        log.warning("entry_blocked_insufficient_margin", required_rs=result.required_margin_rs, available_rs=result.available_margin_rs)
        self.last_notice = (
            f"Margin kam hai: chahiye ₹{result.required_margin_rs:,.0f}, account mein ₹{result.available_margin_rs:,.0f} "
            "- trade nahi lagaya."
        )
        return False

    def _gated(self, is_entry: bool) -> bool:
        reason = self.order_gate(is_entry) if self.order_gate is not None else None
        if reason:
            log.info("orders_gated", is_entry=is_entry, reason=reason)
            self.last_notice = reason
            return True
        return False

    # --- Fill confirmation ------------------------------------------------------------

    def _confirm_fill(self, order_id: int, result: OrderResult, quantity: int, variety: str = "NORMAL") -> tuple[int, float | None, str]:
        """(filled quantity, average price, broker message) once the order is final."""
        if result.status == OrderStatus.FILLED:
            journal.update_order_status(order_id, OrderStatus.FILLED, broker_order_id=result.broker_order_id, filled_price=result.fill_price)
            return quantity, result.fill_price, ""
        if result.status != OrderStatus.OPEN or not result.broker_order_id:
            final = result.status if result.status in FINAL_STATUSES else OrderStatus.REJECTED
            journal.update_order_status(order_id, final, broker_order_id=result.broker_order_id or None, reject_reason=result.message or None)
            return 0, None, result.message

        journal.update_order_status(order_id, OrderStatus.OPEN, broker_order_id=result.broker_order_id)
        state = self._await_final(result.broker_order_id)
        if state is None:
            log.warning("order_not_final_at_timeout_cancelling", broker_order_id=result.broker_order_id)
            self.broker.cancel_order(result.broker_order_id, variety)
            state = self._await_final(result.broker_order_id)
            if state is None:
                journal.update_order_status(order_id, OrderStatus.OPEN, reject_reason="final state unknown after cancel")
                raise OrderStuckError(
                    f"Angel One order {result.broker_order_id} ka status cancel ke baad bhi pakka nahi hua - "
                    "turant Angel One app mein order aur position check karein."
                )

        filled = quantity if state.status == OrderStatus.FILLED and not state.filled_quantity else min(state.filled_quantity, quantity)
        if filled == quantity:
            journal.update_order_status(order_id, OrderStatus.FILLED, filled_price=state.average_price)
            return quantity, state.average_price, ""
        journal.update_order_status(
            order_id, OrderStatus.PARTIALLY_FILLED if filled else state.status, reject_reason=state.message or None
        )
        return filled, state.average_price, state.message

    def _await_final(self, broker_order_id: str) -> OrderState | None:
        deadline = self.clock() + self.fill_timeout_sec
        while True:
            state = self.broker.get_order_state(broker_order_id)
            if state.status in FINAL_STATUSES:
                return state
            if self.clock() >= deadline:
                return None
            self.sleep(self.fill_poll_interval_sec)

    def _close_quantity(
        self, leg_id: int, trading_symbol: str, token: str, leg_side: OrderSide, quantity: int
    ) -> tuple[int, float | None]:
        """MARKET-closes `quantity` of a leg, re-sending only the unfilled remainder of a
        partial fill. A plain rejection is not re-sent here (the caller decides). Returns
        (quantity closed, average price)."""
        side = _opposite(leg_side)
        remaining, notional = quantity, 0.0
        for attempt in range(1, self.exit_attempts + 1):
            order_id = journal.add_order(leg_id, side, "MARKET", remaining)
            result = self.broker.place_order(
                OrderRequest(
                    exchange=EXCHANGE,
                    trading_symbol=trading_symbol,
                    token=token,
                    side=side,
                    quantity=remaining,
                    order_type="MARKET",
                    product_type=self.product_type,
                    tag=order_tag(order_id),
                )
            )
            filled, average_price, message = self._confirm_fill(order_id, result, remaining)
            if filled:
                price = average_price if average_price is not None else self.broker.get_ltp(EXCHANGE, trading_symbol, token)
                notional += filled * price
                remaining -= filled
            if remaining == 0:
                break
            if filled == 0:
                self._leg_message = message or "exit order fill nahi hua"
                break
            log.warning("close_partial_fill_resending_remainder", leg_id=leg_id, remaining=remaining, attempt=attempt)
            self.sleep(self.retry_delay_sec)
        closed = quantity - remaining
        return closed, (round(notional / closed, 2) if closed else None)

    # --- Broker backup SL -------------------------------------------------------------

    def _place_backup_sl(self, leg_id: int, leg: LegIntent, entry_price: float, loss_rs: float) -> None:
        """STOPLOSS_LIMIT buy on a short leg at the price where that leg alone has lost `loss_rs`."""
        trigger = to_tick(entry_price + loss_rs / leg.quantity, up=True)
        limit_price = to_tick(trigger + self.sl_limit_buffer_pts, up=True)
        order_id = journal.add_order(
            leg_id, OrderSide.BUY, "SL", leg.quantity, price=limit_price, trigger_price=trigger, is_safety_net=True
        )
        result = self.broker.place_order(
            OrderRequest(
                exchange=EXCHANGE,
                trading_symbol=leg.trading_symbol,
                token=leg.token,
                side=OrderSide.BUY,
                quantity=leg.quantity,
                order_type="SL",
                product_type=self.product_type,
                price=limit_price,
                trigger_price=trigger,
                variety=SL_VARIETY,
                tag=order_tag(order_id),
            )
        )
        if result.status in (OrderStatus.OPEN, OrderStatus.FILLED) and result.broker_order_id:
            journal.update_order_status(order_id, result.status, broker_order_id=result.broker_order_id, filled_price=result.fill_price)
            log.info("backup_sl_placed", leg_id=leg_id, trigger=trigger, limit=limit_price, broker_order_id=result.broker_order_id)
            return
        journal.update_order_status(
            order_id, result.status if result.status in FINAL_STATUSES else OrderStatus.REJECTED,
            broker_order_id=result.broker_order_id or None, reject_reason=result.message or None,
        )
        log.warning("backup_sl_not_placed", leg_id=leg_id, status=result.status.value, message=result.message)
        self.last_notice = (
            f"Trade laga, par broker par backup SL nahi laga ({result.message or result.status.value}) - "
            "sirf app ka SL chalu hai, app band na karein."
        )

    def backup_sl_triggered(self, strategy_name: str = DEFAULT_STRATEGY_NAME) -> bool:
        """Polled by the app loop while a position is open: has a broker-side backup SL filled?"""
        if self.frozen_reason:
            return False
        position = journal.get_open_position(strategy_name=strategy_name)
        if position is None:
            return False
        triggered = False
        for sl in journal.list_safety_net_orders(position["id"]):
            if sl["status"] == OrderStatus.FILLED:
                triggered = True
                continue
            if sl["status"] not in WORKING_STATUSES or not sl["broker_order_id"]:
                continue
            state = self.broker.get_order_state(sl["broker_order_id"])
            if state.status == OrderStatus.FILLED or state.filled_quantity:
                fully = state.status == OrderStatus.FILLED or state.filled_quantity >= sl["quantity"]
                journal.update_order_status(
                    sl["id"], OrderStatus.FILLED if fully else OrderStatus.PARTIALLY_FILLED, filled_price=state.average_price
                )
                log.warning("backup_sl_triggered_at_broker", leg_id=sl["leg_id"], fill_price=state.average_price)
                triggered = True
            elif state.status in (OrderStatus.REJECTED, OrderStatus.CANCELLED):
                journal.update_order_status(sl["id"], state.status, reject_reason=state.message or None)
        return triggered

    def _stand_down_backup_sls(self, position: dict) -> dict[int, tuple[int, float]]:
        """Cancels every still-working backup SL before legs are closed here - left in place,
        one could trigger afterwards and open a fresh position. Returns {leg_id: (qty, price)}
        for any that already filled: that quantity is already closed at the broker."""
        fills: dict[int, tuple[int, float]] = {}
        for sl in journal.list_safety_net_orders(position["id"]):
            if not sl["broker_order_id"] or sl["status"] not in WORKING_STATUSES | {OrderStatus.FILLED}:
                continue
            if sl["status"] == OrderStatus.FILLED and sl["filled_price"] is not None:
                fills[sl["leg_id"]] = (sl["quantity"], sl["filled_price"])
                continue
            state = self.broker.get_order_state(sl["broker_order_id"])
            if state.status not in FINAL_STATUSES:
                self.broker.cancel_order(sl["broker_order_id"], SL_VARIETY)
                state = self._await_final(sl["broker_order_id"])
                if state is None:
                    raise OrderStuckError(
                        f"Broker backup SL (order {sl['broker_order_id']}) cancel pakka nahi hua - "
                        "Angel One app mein order check karke khud cancel karein."
                    )
            filled = sl["quantity"] if state.status == OrderStatus.FILLED and not state.filled_quantity else state.filled_quantity
            journal.update_order_status(
                sl["id"], OrderStatus.FILLED if filled >= sl["quantity"] else state.status, filled_price=state.average_price
            )
            if filled:
                fills[sl["leg_id"]] = (filled, state.average_price if state.average_price is not None else sl["trigger_price"])
        return fills

    # --- Exit -----------------------------------------------------------

    def execute_exit(
        self,
        intent: ExitIntent,
        daily_loss_limit_rs: float,
        max_consecutive_losses: int,
        trade_date: date | None = None,
        strategy_name: str = DEFAULT_STRATEGY_NAME,
    ) -> bool:
        """True once every leg is closed and the trade is booked. False leaves the position
        OPEN (last_notice says why). `trade_date` defaults to real today; the backtest engine
        passes its simulated day so realized P&L and the circuit breaker scope to that day."""
        self.last_notice = None
        if self.frozen_reason:
            self.last_notice = self.frozen_reason
            return False
        if self._gated(is_entry=False):
            return False
        open_position = journal.get_open_position(strategy_name=strategy_name)
        if open_position is None:
            log.warning("execute_exit_called_with_nothing_open", strategy=strategy_name, reason=intent.reason.value)
            return False

        position_id = open_position["id"]
        journal.update_position_status(position_id, PositionStatus.CLOSING)
        exit_prices = {leg["id"]: leg["exit_price"] for leg in open_position["legs"] if leg["exit_price"] is not None}
        try:
            sl_fills = self._stand_down_backup_sls(open_position)

            # Short (SELL) legs are bought back first, long (BUY) legs sold after - closing a
            # long leg first would briefly leave the short naked, which needs full margin. A
            # leg that never filled on entry (or was already closed) has nothing to close.
            open_legs = [leg for leg in open_position["legs"] if leg["entry_price"] is not None and leg["exit_price"] is None]
            for leg in sorted(open_legs, key=lambda leg: 0 if leg["side"] == OrderSide.SELL else 1):
                done_qty, done_price = sl_fills.get(leg["id"], (0, None))
                closed, price = 0, None
                if leg["quantity"] - done_qty > 0:
                    closed, price = self._close_quantity(
                        leg["id"], leg["trading_symbol"], leg["token"], leg["side"], leg["quantity"] - done_qty
                    )
                total = done_qty + closed
                if total < leg["quantity"]:
                    if total > 0:
                        raise OrderStuckError(
                            f"{leg['trading_symbol']} ka exit adhura raha ({total}/{leg['quantity']} qty) - "
                            "Angel One app mein bachi qty khud band karein."
                        )
                    # Nothing went through - safe to try again next cycle. A short leg failing
                    # stops here so its hedge is never sold out from under it.
                    journal.update_position_status(position_id, PositionStatus.OPEN)
                    label = "Sell leg" if leg["side"] == OrderSide.SELL else "Buy leg"
                    self.last_notice = (
                        f"Exit nahi hua: {label} {leg['trading_symbol']} - {self._leg_message}. "
                        "Position khuli hai, agli check par dobara koshish hogi."
                    )
                    log.error("exit_leg_failed_position_kept_open", position_id=position_id, leg_id=leg["id"])
                    return False
                average = ((done_qty * (done_price or 0.0)) + (closed * (price or 0.0))) / total
                exit_prices[leg["id"]] = round(average, 2)
                journal.update_leg_fill(leg["id"], exit_price=exit_prices[leg["id"]])
        except OrderStuckError as exc:
            journal.update_position_status(position_id, PositionStatus.OPEN)
            self._freeze(str(exc))
            return False

        self._book_closed_position(
            open_position, exit_prices, intent.reason, trade_date, strategy_name, daily_loss_limit_rs, max_consecutive_losses
        )
        return True

    def _book_closed_position(
        self,
        position: dict,
        exit_prices: dict[int, float],
        reason: ExitReason,
        trade_date: date | None,
        strategy_name: str,
        daily_loss_limit_rs: float,
        max_consecutive_losses: int,
    ) -> float:
        realized_pnl = 0.0
        charges_rs = 0.0
        for leg in position["legs"]:
            exit_price = exit_prices.get(leg["id"])
            if leg["entry_price"] is None or exit_price is None:
                continue
            if leg["side"] == OrderSide.BUY:
                realized_pnl += (exit_price - leg["entry_price"]) * leg["quantity"]
            else:
                realized_pnl += (leg["entry_price"] - exit_price) * leg["quantity"]
            if self.charges_config is not None:
                breakdown = estimate_charges_rs(leg["entry_price"], exit_price, leg["quantity"], leg["side"], self.charges_config)
                charges_rs += breakdown.total_rs

        journal.close_position(position["id"], reason, realized_pnl, trade_date, strategy_name=strategy_name, charges_rs=charges_rs)
        log.info(
            "exit_executed",
            position_id=position["id"],
            strategy=strategy_name,
            reason=reason.value,
            realized_pnl_rs=realized_pnl,
            charges_rs=charges_rs,
        )
        circuit_breaker.evaluate_after_trade_close(daily_loss_limit_rs, max_consecutive_losses, trade_date, strategy_name=strategy_name)
        return realized_pnl

    # --- Restart recovery -------------------------------------------------------------

    def recover_after_restart(
        self,
        daily_loss_limit_rs: float,
        max_consecutive_losses: int,
        verify_broker_positions: bool = False,
        strategy_name: str = DEFAULT_STRATEGY_NAME,
    ) -> list[str]:
        """Run once at startup, before anything can send a new order: settles what a crash or
        shutdown left half-done, so trading carries on from the real state.

          1. Orders whose final state was never recorded are looked up at the broker - by id,
             or by their ordertag if the crash hit before the id was saved. One still working
             is cancelled first. Their fills are applied to the legs.
          2. A position left OPENING becomes OPEN if any leg filled (ABORTED if none did); one
             left CLOSING is booked if every leg is closed, otherwise reopened so the normal
             exit path closes the rest.
          3. With verify_broker_positions (live), the journal's open legs are checked against
             the broker's net positions: all flat means it was closed outside the app (booked
             as EXTERNAL); any other mismatch freezes the OMS rather than trade on a wrong picture.

        Never sends a new order itself (only cancels). Returns notices for the dashboard.
        """
        notices: list[str] = []
        try:
            position = journal.get_active_position(strategy_name=strategy_name)
            if position is not None:
                self._settle_unsettled_orders(position)
                position = journal.get_active_position(strategy_name=strategy_name)
                notices += self._resolve_interrupted_status(position, strategy_name, daily_loss_limit_rs, max_consecutive_losses)
            if verify_broker_positions:
                position = journal.get_open_position(strategy_name=strategy_name)
                notices += self._verify_broker_positions(position, strategy_name, daily_loss_limit_rs, max_consecutive_losses)
        except OrderStuckError as exc:
            self._freeze(str(exc))
            notices.append(self.frozen_reason)
        except Exception as exc:  # noqa: BLE001 - an unverifiable start must not trade
            log.exception("startup_recovery_failed")
            self._freeze(f"Startup par Angel One se milaan nahi ho paya ({exc}) - Angel One app mein position aur orders check karein.")
            notices.append(self.frozen_reason)
        for notice in notices:
            log.warning("startup_recovery_notice", notice=notice)
        return notices

    def _settle_unsettled_orders(self, position: dict) -> None:
        legs = {leg["id"]: leg for leg in position["legs"]}
        for order in journal.list_unsettled_orders(position["id"]):
            leg = legs[order["leg_id"]]
            broker_order_id = order["broker_order_id"]
            if broker_order_id:
                state = self.broker.get_order_state(broker_order_id)
            else:
                found = self.broker.find_order_by_tag(order_tag(order["id"]))
                if found is None:
                    journal.update_order_status(order["id"], OrderStatus.REJECTED, reject_reason="not found at the broker after restart")
                    log.info("recovery_order_never_reached_broker", order_id=order["id"])
                    continue
                broker_order_id, state = found

            if state.status not in FINAL_STATUSES:
                self.broker.cancel_order(broker_order_id, SL_VARIETY if order["order_type"] == "SL" else "NORMAL")
                state = self._await_final(broker_order_id)
                if state is None:
                    raise OrderStuckError(
                        f"Restart ke baad order {broker_order_id} ka status pakka nahi hua - Angel One app mein check karein."
                    )

            quantity = order["quantity"]
            filled = quantity if state.status == OrderStatus.FILLED and not state.filled_quantity else min(state.filled_quantity, quantity)
            final = OrderStatus.FILLED if filled == quantity else (OrderStatus.PARTIALLY_FILLED if filled else state.status)
            journal.update_order_status(
                order["id"], final, broker_order_id=broker_order_id, filled_price=state.average_price, reject_reason=state.message or None
            )
            log.warning("recovery_order_settled", order_id=order["id"], broker_order_id=broker_order_id, status=final.value, filled=filled)
            if not filled:
                continue
            if filled < quantity:
                raise OrderStuckError(
                    f"Restart ke baad {leg['trading_symbol']} ka adhura fill mila ({filled}/{quantity} qty) - "
                    "Angel One app mein position check karein."
                )
            price = state.average_price if state.average_price is not None else self.broker.get_ltp(EXCHANGE, leg["trading_symbol"], leg["token"])
            if order["side"] == leg["side"]:
                if leg["entry_price"] is None:
                    journal.update_leg_fill(leg["id"], entry_price=price)
                    leg["entry_price"] = price
            elif leg["exit_price"] is None:
                journal.update_leg_fill(leg["id"], exit_price=price)
                leg["exit_price"] = price

    def _resolve_interrupted_status(
        self, position: dict | None, strategy_name: str, daily_loss_limit_rs: float, max_consecutive_losses: int
    ) -> list[str]:
        if position is None:
            return []
        entered = [leg for leg in position["legs"] if leg["entry_price"] is not None]
        still_open = [leg for leg in entered if leg["exit_price"] is None]

        if position["status"] == PositionStatus.OPENING:
            if not entered:
                journal.update_position_status(position["id"], PositionStatus.ABORTED)
                return ["Restart: pichhli adhuri entry ka koi order fill nahi hua tha - use cancel maan liya."]
            journal.update_position_status(position["id"], PositionStatus.OPEN, set_entry_time=True)
            journal.increment_daily_trade_count(strategy_name=strategy_name)
            return [
                "Restart: pichhli entry beech mein ruki thi, par orders fill ho chuke the - position khuli maan kar "
                "sambhal rahe hain. Legs ek baar Angel One app se mila lein."
            ]

        if position["status"] == PositionStatus.CLOSING:
            if not still_open:
                exit_prices = {leg["id"]: leg["exit_price"] for leg in entered}
                pnl = self._book_closed_position(
                    position, exit_prices, ExitReason.RECOVERED, None, strategy_name, daily_loss_limit_rs, max_consecutive_losses
                )
                return [f"Restart: pichhla exit poora ho chuka tha - trade band kiya (P&L ₹{pnl:,.0f})."]
            journal.update_position_status(position["id"], PositionStatus.OPEN)
            return ["Restart: pichhla exit adhura tha - bachi legs agli check par band hongi."]
        return []

    def _verify_broker_positions(
        self, position: dict | None, strategy_name: str, daily_loss_limit_rs: float, max_consecutive_losses: int
    ) -> list[str]:
        held = {
            p.token: (p.quantity if p.side == OrderSide.BUY else -p.quantity)
            for p in self.broker.get_positions()
            if p.exchange == EXCHANGE
        }
        expected: dict[str, int] = {}
        if position is not None:
            for leg in position["legs"]:
                if leg["entry_price"] is not None and leg["exit_price"] is None:
                    signed = leg["quantity"] if leg["side"] == OrderSide.BUY else -leg["quantity"]
                    expected[leg["token"]] = expected.get(leg["token"], 0) + signed

        notices = []
        if expected:
            if all(held.get(token, 0) == 0 for token in expected):
                exit_prices = {leg["id"]: leg["exit_price"] for leg in position["legs"] if leg["exit_price"] is not None}
                self._book_closed_position(
                    position, exit_prices, ExitReason.EXTERNAL, None, strategy_name, daily_loss_limit_rs, max_consecutive_losses
                )
                notices.append(
                    "Restart: position Angel One par pehle hi band thi (app band rehte broker ne ya aapne band ki) - "
                    "record band kar diya. Asli P&L Angel One app mein dekhein."
                )
            elif any(held.get(token, 0) != qty for token, qty in expected.items()):
                detail = ", ".join(f"{token}: app {qty} / Angel One {held.get(token, 0)}" for token, qty in expected.items())
                raise OrderStuckError(
                    f"Angel One ki positions app ke record se nahi milti ({detail}) - Angel One app mein check karein."
                )
        others = [token for token, qty in held.items() if qty and token not in expected]
        if others:
            notices.append(f"Angel One mein {len(others)} aur F&O position(s) khuli hain jo is app ki nahi - app unhe nahi chhuegi.")
        return notices

    def _freeze(self, reason: str) -> None:
        if self.frozen_reason is None:
            self.frozen_reason = reason + " App ne naye orders rok diye hain - check ke baad app restart karein."
            log.error("oms_frozen", reason=reason)
            circuit_breaker.trigger_kill_switch("order status unknown: " + reason[:150])
        self.last_notice = self.frozen_reason
