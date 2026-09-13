"""Abstract broker interface - paper/live/backtest adapters all implement this.

This is the seam the whole "same strategy/risk/OMS code runs in every mode" design
depends on: nothing above the OMS layer should ever import angelone_rest, paper_broker,
or backtest_broker directly. It only ever talks to a BrokerAdapter. Swapping paper/live/
backtest is just binding a different concrete class here at startup (core/app.py).

Scope is deliberately limited to what the plan has already concretely specified - order
placement (marketable LIMIT for entries, MARKET for exits, SL for the broker-side safety
net), a margin affordability check, LTP, and position lookup. No speculative methods.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal

from angel_auto.core.enums import OrderSide, OrderStatus

OrderType = Literal["LIMIT", "MARKET", "SL"]


@dataclass
class OrderRequest:
    exchange: str
    trading_symbol: str
    token: str
    side: OrderSide
    quantity: int
    order_type: OrderType
    product_type: str  # e.g. "INTRADAY"
    price: float | None = None          # required for LIMIT/SL
    trigger_price: float | None = None  # required for SL
    variety: str = "NORMAL"
    tag: str = ""                       # correlation id back to our own order/position records


@dataclass
class OrderResult:
    broker_order_id: str
    status: OrderStatus
    fill_price: float | None = None  # set when status == FILLED
    message: str = ""


@dataclass
class OrderState:
    """Where a placed order stands at the broker right now. A live broker acknowledges an
    order as OPEN and only reports the fill later, so callers poll this until the status is
    final (FILLED / REJECTED / CANCELLED) - `filled_quantity` can be non-zero on a CANCELLED
    or still-OPEN order (a partial fill)."""

    status: OrderStatus
    filled_quantity: int = 0
    average_price: float | None = None
    message: str = ""


@dataclass
class MarginLeg:
    exchange: str
    trading_symbol: str
    token: str
    side: OrderSide
    quantity: int
    product_type: str


@dataclass
class MarginCheckResult:
    required_margin_rs: float
    available_margin_rs: float
    is_affordable: bool
    raw_response: dict = field(default_factory=dict)


@dataclass
class PositionSnapshot:
    exchange: str
    trading_symbol: str
    token: str
    side: OrderSide
    quantity: int
    average_price: float
    ltp: float


class BrokerAdapter(ABC):
    """One instance per running app, bound to whatever `mode` config.yaml specifies."""

    @abstractmethod
    def place_order(self, request: OrderRequest) -> OrderResult: ...

    @abstractmethod
    def cancel_order(self, broker_order_id: str, variety: str = "NORMAL") -> None: ...

    @abstractmethod
    def get_order_state(self, broker_order_id: str) -> OrderState:
        """Must never report an order the broker may still fill as REJECTED/CANCELLED - when
        the answer is unknown (API failure, not in the book yet), report OPEN."""
        ...

    def get_order_status(self, broker_order_id: str) -> OrderStatus:
        return self.get_order_state(broker_order_id).status

    def find_order_by_tag(self, tag: str) -> tuple[str, OrderState] | None:
        """(broker order id, state) of the latest order sent with this tag, None if there is
        none. Raises if the broker can't be asked - "unknown" must not read as "never sent".
        Brokers that keep no cross-restart record (paper, backtest) have nothing to find."""
        return None

    @abstractmethod
    def get_ltp(self, exchange: str, trading_symbol: str, token: str) -> float: ...

    @abstractmethod
    def check_margin(self, legs: list[MarginLeg]) -> MarginCheckResult:
        """Go/no-go affordability check for a proposed combo - the real sizing gate
        for the flagship strategy (fixed at 1 lot; this decides whether that lot is
        even affordable, never scales it up or down)."""
        ...

    @abstractmethod
    def get_positions(self) -> list[PositionSnapshot]: ...
