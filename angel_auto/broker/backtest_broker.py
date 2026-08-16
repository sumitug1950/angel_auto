"""Simulated broker for backtesting - fills against Black-Scholes-synthesized theoretical
option prices (that backtest day's spot/VIX -> IV, plus the leg's strike/expiry), rather
than real historical per-contract prices. Real historical data for the many specific
option contracts a multi-month backtest would roll through isn't practically available
(see backtest/data_loader.py) - this is the documented approximation instead.
"""
from __future__ import annotations

import itertools
from datetime import date, datetime

from angel_auto.analytics.black_scholes import bs_price
from angel_auto.broker.base import (
    BrokerAdapter,
    MarginCheckResult,
    MarginLeg,
    OrderRequest,
    OrderResult,
    PositionSnapshot,
)
from angel_auto.core.enums import OrderSide, OrderStatus


class BacktestBroker(BrokerAdapter):
    def __init__(self, rate: float = 0.065, starting_capital_rs: float = 20000.0) -> None:
        self.rate = rate
        self.available_margin_rs = starting_capital_rs
        self._order_counter = itertools.count(1)
        self.current_date: date = date.today()
        self.current_spot: float = 0.0
        self.current_vix: float = 0.0
        self._token_meta: dict[str, tuple[float, str, str]] = {}  # token -> (strike, option_type, expiry)

    def register_token(self, token: str, strike: float, option_type: str, expiry: str) -> None:
        self._token_meta[token] = (strike, option_type, expiry)

    def set_market_state(self, as_of: date, spot: float, vix: float) -> None:
        self.current_date = as_of
        self.current_spot = spot
        self.current_vix = vix

    def _theoretical_price(self, token: str) -> float | None:
        meta = self._token_meta.get(token)
        if meta is None or self.current_spot <= 0:
            return None
        strike, option_type, expiry = meta
        expiry_date = datetime.strptime(expiry, "%d%b%Y").date()
        days_remaining = (expiry_date - self.current_date).days
        # Clamp to a small positive floor rather than rejecting at exactly 0 (or negative)
        # days remaining - the mandatory square-off fires ON expiry day itself, and an
        # option expiring today still has a real (mostly intrinsic) value that needs to be
        # priced so the exit can actually fill, not treated as already-worthless/invalid.
        time_to_expiry_years = max(days_remaining, 0.5) / 365.0
        iv = max(self.current_vix / 100.0, 0.01)
        return bs_price(self.current_spot, strike, time_to_expiry_years, self.rate, iv, option_type)

    def place_order(self, request: OrderRequest) -> OrderResult:
        broker_order_id = f"BT{next(self._order_counter):08d}"
        price = self._theoretical_price(request.token)
        if price is None or price <= 0:
            return OrderResult(broker_order_id, OrderStatus.REJECTED, message="no theoretical price available")
        return OrderResult(broker_order_id, OrderStatus.FILLED, fill_price=round(price, 2))

    def cancel_order(self, broker_order_id: str, variety: str = "NORMAL") -> None:
        pass  # backtest fills are always immediate - nothing to cancel

    def get_order_status(self, broker_order_id: str) -> OrderStatus:
        return OrderStatus.FILLED

    def get_ltp(self, exchange: str, trading_symbol: str, token: str) -> float:
        return self._theoretical_price(token) or 0.0

    def check_margin(self, legs: list[MarginLeg]) -> MarginCheckResult:
        required = 0.0
        for leg in legs:
            price = self._theoretical_price(leg.token) or 0.0
            required += price * leg.quantity if leg.side == OrderSide.BUY else price * leg.quantity * 1.5
        return MarginCheckResult(
            required_margin_rs=required,
            available_margin_rs=self.available_margin_rs,
            is_affordable=required <= self.available_margin_rs,
        )

    def get_positions(self) -> list[PositionSnapshot]:
        return []
