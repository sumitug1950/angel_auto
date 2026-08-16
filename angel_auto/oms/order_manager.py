"""Order Manager - turns strategy intents (EntryIntent/ExitIntent) into real orders via a
BrokerAdapter (paper today, live in Phase 10 - same interface), enforcing the leg-sequencing
and fill-failure rules from the plan:

  CREDIT structure: hedge (OTM buy) sent first, ITM short only after the hedge is confirmed
  filled - never briefly naked. Hedge fails -> abort entirely, ITM short never sent. If the
  ITM short somehow fills before a hedge failure is caught, emergency-exit it immediately.

  DEBIT structure: ITM buy sent first, then OTM sell - lower risk ordering, since the worst
  case (only ITM fills) is just a plain long option, not a naked short. OTM sell failing
  after ITM filled -> retry a few times, then leave the ITM leg open as a standalone
  position (already protected by the strategy's SL/trailing) rather than force-unwind a
  safe position.

Order type: marketable LIMIT for entries (LTP +/- a small buffer so it fills fast without
risking a blind MARKET print on a thin strike), MARKET for every software-triggered exit
(SL/trailing/manual/square-off - speed over price precision when getting out).
"""
from __future__ import annotations

import time
from datetime import date

from angel_auto.analytics.charges import estimate_charges_rs
from angel_auto.broker.base import BrokerAdapter, OrderRequest
from angel_auto.core.enums import ExitReason, OrderSide, OrderStatus, PositionStatus, StructureType
from angel_auto.logging_conf import get_logger
from angel_auto.persistence import journal
from angel_auto.risk import circuit_breaker
from angel_auto.settings import ChargesConfig
from angel_auto.strategy.base import EntryIntent, ExitIntent, LegIntent

log = get_logger(__name__)

EXCHANGE = "NFO"
DEFAULT_STRATEGY_NAME = "macd_itm_otm_spread"


class OrderManager:
    def __init__(
        self,
        broker: BrokerAdapter,
        product_type: str = "INTRADAY",
        entry_slippage_buffer_pts: float = 1.0,
        max_otm_retry_attempts: int = 3,
        retry_delay_sec: float = 1.0,
        charges_config: ChargesConfig | None = None,
    ) -> None:
        self.broker = broker
        self.product_type = product_type
        self.entry_slippage_buffer_pts = entry_slippage_buffer_pts
        self.max_otm_retry_attempts = max_otm_retry_attempts
        self.retry_delay_sec = retry_delay_sec
        self.charges_config = charges_config

    # --- Entry --------------------------------------------------------------

    def execute_entry(
        self, intent: EntryIntent, trade_date: date | None = None, strategy_name: str = DEFAULT_STRATEGY_NAME
    ) -> int | None:
        """Returns the new position's id on success, None if the entry was aborted.
        `trade_date` defaults to real today; the backtest engine passes its simulated day
        so the daily trade-count counter scopes to the day being replayed."""
        position_id = journal.create_position(
            intent.direction,
            intent.structure_type,
            intent.expiry,
            direction_request_id=intent.direction_request_id,
            iv_rank_at_entry=intent.iv_rank,
            strategy_name=strategy_name,
        )

        if len(intent.legs) == 1:
            return self._execute_single_leg_entry(position_id, intent, trade_date, strategy_name)

        itm_leg = next(leg for leg in intent.legs if leg.role == "ITM")
        otm_leg = next(leg for leg in intent.legs if leg.role == "OTM")

        if intent.structure_type == StructureType.CREDIT:
            first_leg, second_leg = otm_leg, itm_leg  # hedge first
        else:
            first_leg, second_leg = itm_leg, otm_leg  # ITM first

        first_leg_id = self._persist_leg(position_id, first_leg)
        first_fill = self._place_entry_leg(first_leg_id, first_leg)
        if first_fill is None:
            log.error("entry_aborted_first_leg_failed", position_id=position_id, role=first_leg.role)
            journal.update_position_status(position_id, PositionStatus.ABORTED)
            return None
        journal.update_leg_fill(first_leg_id, entry_price=first_fill)

        second_leg_id = self._persist_leg(position_id, second_leg)
        second_fill = self._place_entry_leg(second_leg_id, second_leg)

        if second_fill is None:
            if intent.structure_type == StructureType.CREDIT:
                log.warning("entry_second_leg_failed_unwinding_hedge", position_id=position_id)
                self._emergency_exit_leg(first_leg_id, first_leg)
                journal.update_position_status(position_id, PositionStatus.ABORTED)
                return None
            second_fill = self._retry_leg(second_leg_id, second_leg)
            if second_fill is None:
                log.warning("entry_otm_leg_unfilled_itm_stands_alone", position_id=position_id)

        if second_fill is not None:
            journal.update_leg_fill(second_leg_id, entry_price=second_fill)

        journal.update_position_status(position_id, PositionStatus.OPEN, set_entry_time=True)
        journal.increment_daily_trade_count(trade_date, strategy_name=strategy_name)
        log.info("entry_executed", position_id=position_id, strategy=strategy_name, structure=intent.structure_type.value)
        return position_id

    def _execute_single_leg_entry(
        self, position_id: int, intent: EntryIntent, trade_date: date | None, strategy_name: str
    ) -> int | None:
        """A one-leg entry (the zero-cross strategies) - no partner-leg-cleanup concern
        since there's nothing to unwind, so a failed fill just retries like the flagship's
        OTM leg does."""
        leg = intent.legs[0]
        leg_id = self._persist_leg(position_id, leg)
        fill = self._place_entry_leg(leg_id, leg)
        if fill is None:
            fill = self._retry_leg(leg_id, leg)
        if fill is None:
            log.error("entry_aborted_single_leg_failed", position_id=position_id, strategy=strategy_name)
            journal.update_position_status(position_id, PositionStatus.ABORTED)
            return None

        journal.update_leg_fill(leg_id, entry_price=fill)
        journal.update_position_status(position_id, PositionStatus.OPEN, set_entry_time=True)
        journal.increment_daily_trade_count(trade_date, strategy_name=strategy_name)
        log.info("entry_executed", position_id=position_id, strategy=strategy_name, structure="SINGLE_LEG")
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

    def _place_entry_leg(self, leg_id: int, leg: LegIntent) -> float | None:
        ltp = self.broker.get_ltp(EXCHANGE, leg.trading_symbol, leg.token)
        if ltp <= 0:
            order_id = journal.add_order(leg_id, leg.side, "LIMIT", leg.quantity)
            journal.update_order_status(order_id, OrderStatus.REJECTED, reject_reason="no live quote")
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
                tag=f"leg:{leg_id}",
            )
        )

        if result.status == OrderStatus.FILLED:
            journal.update_order_status(order_id, OrderStatus.FILLED, broker_order_id=result.broker_order_id, filled_price=result.fill_price)
            return result.fill_price

        journal.update_order_status(order_id, result.status, broker_order_id=result.broker_order_id, reject_reason=result.message)
        if result.status == OrderStatus.OPEN:
            self.broker.cancel_order(result.broker_order_id)
            journal.update_order_status(order_id, OrderStatus.CANCELLED)
        return None

    def _retry_leg(self, leg_id: int, leg: LegIntent) -> float | None:
        for attempt in range(1, self.max_otm_retry_attempts + 1):
            log.info("entry_leg_retry", leg_id=leg_id, attempt=attempt)
            time.sleep(self.retry_delay_sec)
            fill_price = self._place_entry_leg(leg_id, leg)
            if fill_price is not None:
                return fill_price
        return None

    def _emergency_exit_leg(self, leg_id: int, leg: LegIntent) -> None:
        """Unwinds a single filled leg immediately - used when its partner leg failed and
        leaving this one open would be dangerous (e.g. a naked short from a failed hedge)."""
        opposite_side = OrderSide.SELL if leg.side == OrderSide.BUY else OrderSide.BUY
        order_id = journal.add_order(leg_id, opposite_side, "MARKET", leg.quantity)
        result = self.broker.place_order(
            OrderRequest(
                exchange=EXCHANGE,
                trading_symbol=leg.trading_symbol,
                token=leg.token,
                side=opposite_side,
                quantity=leg.quantity,
                order_type="MARKET",
                product_type=self.product_type,
                tag=f"emergency_exit:{leg_id}",
            )
        )
        if result.status == OrderStatus.FILLED:
            journal.update_order_status(order_id, OrderStatus.FILLED, broker_order_id=result.broker_order_id, filled_price=result.fill_price)
            journal.update_leg_fill(leg_id, exit_price=result.fill_price)
            log.info("emergency_exit_leg_filled", leg_id=leg_id, fill_price=result.fill_price)
        else:
            journal.update_order_status(order_id, result.status, broker_order_id=result.broker_order_id, reject_reason=result.message)
            log.error("emergency_exit_leg_failed", leg_id=leg_id, status=result.status.value)

    def _marketable_limit_price(self, ltp: float, side: OrderSide) -> float:
        buffer = self.entry_slippage_buffer_pts
        return round(ltp + buffer, 2) if side == OrderSide.BUY else round(ltp - buffer, 2)

    # --- Exit -----------------------------------------------------------

    def execute_exit(
        self,
        intent: ExitIntent,
        daily_loss_limit_rs: float,
        max_consecutive_losses: int,
        trade_date: date | None = None,
        strategy_name: str = DEFAULT_STRATEGY_NAME,
    ) -> None:
        """`trade_date` defaults to real today; the backtest engine passes its simulated
        day so realized P&L and the circuit breaker scope to the day being replayed."""
        open_position = journal.get_open_position(strategy_name=strategy_name)
        if open_position is None:
            log.warning("execute_exit_called_with_nothing_open", strategy=strategy_name, reason=intent.reason.value)
            return

        journal.update_position_status(open_position["id"], PositionStatus.CLOSING)

        realized_pnl = 0.0
        charges_rs = 0.0
        for leg in open_position["legs"]:
            fill_price = self._place_exit_leg(leg)
            if fill_price is None:
                log.error("exit_leg_failed", position_id=open_position["id"], leg_id=leg["id"])
                continue
            journal.update_leg_fill(leg["id"], exit_price=fill_price)
            entry_price = leg["entry_price"] or 0.0
            if leg["side"] == OrderSide.BUY:
                realized_pnl += (fill_price - entry_price) * leg["quantity"]
            else:
                realized_pnl += (entry_price - fill_price) * leg["quantity"]
            if self.charges_config is not None:
                breakdown = estimate_charges_rs(entry_price, fill_price, leg["quantity"], leg["side"], self.charges_config)
                charges_rs += breakdown.total_rs

        journal.close_position(
            open_position["id"], intent.reason, realized_pnl, trade_date, strategy_name=strategy_name, charges_rs=charges_rs
        )
        log.info(
            "exit_executed",
            position_id=open_position["id"],
            strategy=strategy_name,
            reason=intent.reason.value,
            realized_pnl_rs=realized_pnl,
            charges_rs=charges_rs,
        )

        circuit_breaker.evaluate_after_trade_close(
            daily_loss_limit_rs, max_consecutive_losses, trade_date, strategy_name=strategy_name
        )

    def _place_exit_leg(self, leg: dict) -> float | None:
        opposite_side = OrderSide.SELL if leg["side"] == OrderSide.BUY else OrderSide.BUY
        order_id = journal.add_order(leg["id"], opposite_side, "MARKET", leg["quantity"])
        result = self.broker.place_order(
            OrderRequest(
                exchange=EXCHANGE,
                trading_symbol=leg["trading_symbol"],
                token=leg["token"],
                side=opposite_side,
                quantity=leg["quantity"],
                order_type="MARKET",
                product_type=self.product_type,
                tag=f"exit:{leg['id']}",
            )
        )
        if result.status == OrderStatus.FILLED:
            journal.update_order_status(order_id, OrderStatus.FILLED, broker_order_id=result.broker_order_id, filled_price=result.fill_price)
            return result.fill_price
        journal.update_order_status(order_id, result.status, broker_order_id=result.broker_order_id, reject_reason=result.message)
        return None
