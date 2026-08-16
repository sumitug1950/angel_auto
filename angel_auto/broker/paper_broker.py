"""Simulated broker for paper trading - the same BrokerAdapter interface the real Angel One
adapter will implement (Phase 10), but fills happen instantly against the live option-chain
snapshot's LTP (plus a small slippage model) instead of a real order book. No network calls,
no real orders - this is what proves the strategy/OMS logic out before touching real money.
"""
from __future__ import annotations

import itertools

from angel_auto.broker.base import (
    BrokerAdapter,
    MarginCheckResult,
    MarginLeg,
    OrderRequest,
    OrderResult,
    PositionSnapshot,
)
from angel_auto.core.enums import OrderSide, OrderStatus
from angel_auto.data.market_data import OptionChainSnapshot
from angel_auto.logging_conf import get_logger

log = get_logger(__name__)


class PaperBroker(BrokerAdapter):
    def __init__(self, option_chain: OptionChainSnapshot, starting_capital_rs: float, slippage_pct: float = 0.1) -> None:
        self.option_chain = option_chain
        self.available_margin_rs = starting_capital_rs
        self.slippage_pct = slippage_pct
        self._order_counter = itertools.count(1)
        self._orders: dict[str, dict] = {}
        self._positions: dict[str, PositionSnapshot] = {}

    def place_order(self, request: OrderRequest) -> OrderResult:
        broker_order_id = f"PAPER{next(self._order_counter):08d}"
        quote = self.option_chain.get(request.token)

        if quote is None or quote.ltp <= 0:
            self._orders[broker_order_id] = {"status": OrderStatus.REJECTED}
            log.warning("paper_order_rejected_no_quote", token=request.token, symbol=request.trading_symbol)
            return OrderResult(broker_order_id, OrderStatus.REJECTED, message="no live quote for token")

        fill_price = self._simulate_fill_price(quote.ltp, request.side)

        if request.order_type == "LIMIT" and request.price is not None and not self._limit_is_marketable(
            request.side, request.price, fill_price
        ):
            self._orders[broker_order_id] = {"status": OrderStatus.OPEN}
            log.info("paper_order_left_open_limit_not_marketable", broker_order_id=broker_order_id, token=request.token)
            return OrderResult(broker_order_id, OrderStatus.OPEN, message="limit price not marketable yet")

        self._orders[broker_order_id] = {
            "status": OrderStatus.FILLED,
            "fill_price": fill_price,
            "token": request.token,
            "side": request.side,
            "quantity": request.quantity,
        }
        self._apply_fill_to_position(request, fill_price)
        log.info(
            "paper_order_filled",
            broker_order_id=broker_order_id,
            token=request.token,
            side=request.side.value,
            fill_price=fill_price,
            quantity=request.quantity,
        )
        return OrderResult(broker_order_id, OrderStatus.FILLED, fill_price=fill_price)

    def _simulate_fill_price(self, ltp: float, side: OrderSide) -> float:
        slip = ltp * (self.slippage_pct / 100.0)
        # buying costs a touch more than LTP, selling gets a touch less - a simple spread model
        return round(ltp + slip, 2) if side == OrderSide.BUY else round(ltp - slip, 2)

    @staticmethod
    def _limit_is_marketable(side: OrderSide, limit_price: float, fill_price: float) -> bool:
        return fill_price <= limit_price if side == OrderSide.BUY else fill_price >= limit_price

    def cancel_order(self, broker_order_id: str, variety: str = "NORMAL") -> None:
        order = self._orders.get(broker_order_id)
        if order and order["status"] == OrderStatus.OPEN:
            order["status"] = OrderStatus.CANCELLED
            log.info("paper_order_cancelled", broker_order_id=broker_order_id)

    def get_order_status(self, broker_order_id: str) -> OrderStatus:
        order = self._orders.get(broker_order_id)
        return order["status"] if order else OrderStatus.REJECTED

    def get_ltp(self, exchange: str, trading_symbol: str, token: str) -> float:
        quote = self.option_chain.get(token)
        return quote.ltp if quote else 0.0

    def check_margin(self, legs: list[MarginLeg]) -> MarginCheckResult:
        """Paper-mode approximation only - good enough to validate strategy/OMS logic.
        The live broker's real margin API (Phase 10) is the actual gate once real money
        is involved; this never should be trusted for a live sizing decision."""
        required = 0.0
        for leg in legs:
            quote = self.option_chain.get(leg.token)
            price = quote.ltp if quote else 0.0
            if leg.side == OrderSide.BUY:
                required += price * leg.quantity
            else:
                required += price * leg.quantity * 1.5  # rough SPAN-ish placeholder for a short leg
        return MarginCheckResult(
            required_margin_rs=required,
            available_margin_rs=self.available_margin_rs,
            is_affordable=required <= self.available_margin_rs,
        )

    def get_positions(self) -> list[PositionSnapshot]:
        return list(self._positions.values())

    def _apply_fill_to_position(self, request: OrderRequest, fill_price: float) -> None:
        existing = self._positions.get(request.token)
        signed_qty = request.quantity if request.side == OrderSide.BUY else -request.quantity
        if existing is None:
            self._positions[request.token] = PositionSnapshot(
                exchange=request.exchange,
                trading_symbol=request.trading_symbol,
                token=request.token,
                side=request.side,
                quantity=request.quantity,
                average_price=fill_price,
                ltp=fill_price,
            )
            return

        existing_signed = existing.quantity if existing.side == OrderSide.BUY else -existing.quantity
        new_signed = existing_signed + signed_qty
        if new_signed == 0:
            del self._positions[request.token]
        else:
            existing.quantity = abs(new_signed)
            existing.side = OrderSide.BUY if new_signed > 0 else OrderSide.SELL
            existing.ltp = fill_price
