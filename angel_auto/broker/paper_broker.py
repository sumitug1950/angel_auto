"""Simulated broker for paper trading - the same BrokerAdapter interface the real Angel One
adapter implements, but fills happen against the live option-chain snapshot's LTP (plus a
small slippage model) instead of a real order book. No network calls, no real orders.

Marketable orders fill instantly. A resting LIMIT order or a STOPLOSS_LIMIT order stays OPEN
and is re-evaluated against the current LTP every time its state is read, so the OMS's
fill polling and broker backup SL logic run the same way they do live.
"""
from __future__ import annotations

import itertools

from angel_auto.broker.base import (
    BrokerAdapter,
    MarginCheckResult,
    MarginLeg,
    OrderRequest,
    OrderResult,
    OrderState,
    PositionSnapshot,
)
from angel_auto.core.enums import OrderSide, OrderStatus
from angel_auto.data.market_data import OptionChainSnapshot
from angel_auto.logging_conf import get_logger

log = get_logger(__name__)

NAKED_SHORT_MARGIN_PCT = 0.10  # rough stand-in for SPAN + exposure on an unhedged short


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
            self._orders[broker_order_id] = {"status": OrderStatus.REJECTED, "message": "no live quote for token"}
            log.warning("paper_order_rejected_no_quote", token=request.token, symbol=request.trading_symbol)
            return OrderResult(broker_order_id, OrderStatus.REJECTED, message="no live quote for token")

        if request.order_type == "SL":
            self._orders[broker_order_id] = {"status": OrderStatus.OPEN, "request": request}
            log.info("paper_stop_loss_resting", broker_order_id=broker_order_id, trigger=request.trigger_price)
            return OrderResult(broker_order_id, OrderStatus.OPEN)

        fill_price = self._simulate_fill_price(quote.ltp, request.side)
        if request.order_type == "LIMIT" and request.price is not None and not self._limit_is_marketable(
            request.side, request.price, fill_price
        ):
            self._orders[broker_order_id] = {"status": OrderStatus.OPEN, "request": request}
            log.info("paper_order_left_open_limit_not_marketable", broker_order_id=broker_order_id, token=request.token)
            return OrderResult(broker_order_id, OrderStatus.OPEN, message="limit price not marketable yet")

        return self._fill(broker_order_id, request, fill_price)

    def _fill(self, broker_order_id: str, request: OrderRequest, fill_price: float) -> OrderResult:
        self._orders[broker_order_id] = {"status": OrderStatus.FILLED, "fill_price": fill_price, "request": request}
        self._apply_fill_to_position(request, fill_price)
        log.info(
            "paper_order_filled",
            broker_order_id=broker_order_id,
            token=request.token,
            side=request.side.value,
            order_type=request.order_type,
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

    def get_order_state(self, broker_order_id: str) -> OrderState:
        order = self._orders.get(broker_order_id)
        if order is None:
            return OrderState(OrderStatus.REJECTED, message="unknown order")

        request = order.get("request")
        if order["status"] == OrderStatus.OPEN and request is not None:
            quote = self.option_chain.get(request.token)
            ltp = quote.ltp if quote is not None else 0.0
            if ltp > 0:
                if request.order_type == "SL":
                    buy = request.side == OrderSide.BUY
                    if (buy and ltp >= request.trigger_price) or (not buy and ltp <= request.trigger_price):
                        self._fill(broker_order_id, request, self._simulate_fill_price(ltp, request.side))
                else:
                    fill_price = self._simulate_fill_price(ltp, request.side)
                    if request.price is None or self._limit_is_marketable(request.side, request.price, fill_price):
                        self._fill(broker_order_id, request, fill_price)

        order = self._orders[broker_order_id]
        if order["status"] == OrderStatus.FILLED:
            return OrderState(OrderStatus.FILLED, order["request"].quantity, order["fill_price"])
        return OrderState(order["status"], message=order.get("message", ""))

    def cancel_order(self, broker_order_id: str, variety: str = "NORMAL") -> None:
        order = self._orders.get(broker_order_id)
        if order and order["status"] == OrderStatus.OPEN:
            order["status"] = OrderStatus.CANCELLED
            log.info("paper_order_cancelled", broker_order_id=broker_order_id)

    def get_ltp(self, exchange: str, trading_symbol: str, token: str) -> float:
        quote = self.option_chain.get(token)
        return quote.ltp if quote else 0.0

    def check_margin(self, legs: list[MarginLeg]) -> MarginCheckResult:
        """Paper-mode approximation of an F&O basket margin - the live broker's margin API is
        the real gate. Net premium paid counts in full; a short covered by a long of the same
        type that is further in-the-money costs nothing more (debit spread); a short whose
        hedge is further out costs the strike width minus the credit (credit spread); an
        unhedged short costs NAKED_SHORT_MARGIN_PCT of its notional."""
        quoted = [(leg, self.option_chain.get(leg.token)) for leg in legs]
        quoted = [(leg, quote) for leg, quote in quoted if quote is not None]
        net_premium = sum(quote.ltp * leg.quantity * (1 if leg.side == OrderSide.BUY else -1) for leg, quote in quoted)
        longs = [quote for leg, quote in quoted if leg.side == OrderSide.BUY]

        risk = 0.0
        for leg, short in quoted:
            if leg.side != OrderSide.SELL:
                continue
            hedge = next((q for q in longs if q.option_type == short.option_type), None)
            if hedge is None:
                risk += short.strike * leg.quantity * NAKED_SHORT_MARGIN_PCT
                continue
            hedge_further_out = hedge.strike > short.strike if short.option_type == "CE" else hedge.strike < short.strike
            if hedge_further_out:
                risk += abs(hedge.strike - short.strike) * leg.quantity

        required = max(net_premium + risk, 0.0)
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
