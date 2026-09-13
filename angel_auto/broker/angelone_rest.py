"""Live Angel One REST broker adapter - real order placement, built to the exact same
BrokerAdapter interface paper/backtest modes use.

CAUTION: place_order() here sends a REAL order to a REAL account when `mode: live`.
Behaviour confirmed live against the real account (2026-09-13, market closed so every order
became an AMO and was cancelled): LIMIT buy/sell and STOPLOSS_LIMIT orders are accepted,
`ordertag` is echoed back in the order book and order details, cancel works for all three,
the order book fields parsed below are the real ones, and the order book endpoint answers
"exceeding access rate" when polled faster than about every couple of seconds - which is why
single-order status goes through individual_order_details and the order book is throttled.
Orders only go out from the static IP registered on the SmartAPI dashboard (SEBI 2026 rules).
"""
from __future__ import annotations

import threading
import time

from angel_auto.broker.angelone_auth import AngelSession
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
from angel_auto.logging_conf import get_logger

log = get_logger(__name__)

_ORDER_TYPE_MAP = {"LIMIT": "LIMIT", "MARKET": "MARKET", "SL": "STOPLOSS_LIMIT"}
# Anything not listed (validation pending, modify pending, put order req received, ...) is
# treated as still working - never as a final state.
_STATUS_MAP = {
    "open": OrderStatus.OPEN,
    "open pending": OrderStatus.OPEN,
    "pending": OrderStatus.OPEN,
    "trigger pending": OrderStatus.OPEN,
    "complete": OrderStatus.FILLED,
    "cancelled": OrderStatus.CANCELLED,
    "rejected": OrderStatus.REJECTED,
}
SL_VARIETY = "STOPLOSS"  # SmartAPI requires this variety for STOPLOSS_LIMIT orders
ORDER_BOOK_MIN_INTERVAL_SEC = 2.5


def _state_from_row(row: dict) -> OrderState:
    """One order book / order details row -> OrderState."""
    raw_status = (row.get("orderstatus") or row.get("status") or "").lower()
    status = _STATUS_MAP.get(raw_status, OrderStatus.OPEN)
    filled = int(float(row.get("filledshares") or 0))
    average = float(row.get("averageprice") or 0) or None
    if status == OrderStatus.OPEN and filled > 0:
        status = OrderStatus.PARTIALLY_FILLED
    return OrderState(status, filled_quantity=filled, average_price=average, message=row.get("text") or "")


class AngelOneBroker(BrokerAdapter):
    """Real order placement via Angel One SmartAPI. `session` must already be logged in
    (see broker/angelone_auth.py) - this class only ever uses an existing session, and the
    app swaps in a fresh one after a re-login."""

    def __init__(self, session: AngelSession, order_book_min_interval_sec: float = ORDER_BOOK_MIN_INTERVAL_SEC) -> None:
        self.session = session
        self.order_book_min_interval_sec = order_book_min_interval_sec
        self._unique_ids: dict[str, str] = {}  # orderid -> uniqueorderid, for the per-order details endpoint
        self._order_book_lock = threading.Lock()
        self._last_order_book_at = 0.0

    def place_order(self, request: OrderRequest) -> OrderResult:
        params = {
            "variety": SL_VARIETY if request.order_type == "SL" else request.variety,
            "tradingsymbol": request.trading_symbol,
            "symboltoken": request.token,
            "transactiontype": request.side.value,
            "exchange": request.exchange,
            "ordertype": _ORDER_TYPE_MAP[request.order_type],
            "producttype": request.product_type,
            "duration": "DAY",
            "quantity": str(request.quantity),
            "price": str(request.price) if request.price is not None else "0",
        }
        if request.order_type == "SL" and request.trigger_price is not None:
            params["triggerprice"] = str(request.trigger_price)
        if request.tag:
            params["ordertag"] = request.tag  # lets a restart find an order whose id was never saved

        try:
            response = self.session.smart_connect.placeOrderFullResponse(params)
        except Exception as exc:  # noqa: BLE001 - a broker/network failure must not crash the OMS
            log.error("angelone_place_order_exception", error=str(exc), symbol=request.trading_symbol)
            return OrderResult("", OrderStatus.REJECTED, message=str(exc))

        if not response or not response.get("status"):
            message = (response or {}).get("message", "unknown error")
            log.error("angelone_place_order_rejected", message=message, symbol=request.trading_symbol)
            return OrderResult("", OrderStatus.REJECTED, message=message)

        data = response["data"]
        order_id = data["orderid"]
        if data.get("uniqueorderid"):
            self._unique_ids[order_id] = data["uniqueorderid"]
        log.info("angelone_order_placed", order_id=order_id, symbol=request.trading_symbol, side=request.side.value, tag=request.tag)
        # SmartAPI doesn't return a synchronous fill - the order is OPEN until its status says
        # otherwise. The OMS polls get_order_state() until it's final.
        return OrderResult(order_id, OrderStatus.OPEN)

    def cancel_order(self, broker_order_id: str, variety: str = "NORMAL") -> None:
        response = self.session.smart_connect.cancelOrder(broker_order_id, variety)
        if not response or not response.get("status"):
            log.error(
                "angelone_cancel_order_failed", order_id=broker_order_id, message=(response or {}).get("message")
            )

    def get_order_state(self, broker_order_id: str) -> OrderState:
        unique_id = self._unique_ids.get(broker_order_id)
        if unique_id:
            try:
                response = self.session.smart_connect.individual_order_details(unique_id)
                if response and response.get("status") and response.get("data"):
                    return _state_from_row(response["data"])
            except Exception as exc:  # noqa: BLE001 - fall back to the order book below
                log.warning("angelone_order_details_exception", error=str(exc), order_id=broker_order_id)

        rows = self._order_book_rows()
        if rows is None:  # unknown, not rejected: the order may still fill
            return OrderState(OrderStatus.OPEN, message="order book unavailable")
        for row in rows:
            if row.get("orderid") == broker_order_id:
                return _state_from_row(row)
        # Right after placement an order can take a moment to show up in the book.
        return OrderState(OrderStatus.OPEN, message="not in order book yet")

    def find_order_by_tag(self, tag: str) -> tuple[str, OrderState] | None:
        rows = self._order_book_rows()
        if rows is None:
            raise RuntimeError("Angel One order book unavailable")
        for row in reversed(rows):
            if row.get("ordertag") == tag:
                return row["orderid"], _state_from_row(row)
        return None

    def _order_book_rows(self) -> list[dict] | None:
        """The day's order book, or None when it couldn't be read. Calls are spaced at least
        order_book_min_interval_sec apart - faster polling gets "exceeding access rate"."""
        with self._order_book_lock:
            wait = self.order_book_min_interval_sec - (time.monotonic() - self._last_order_book_at)
            if wait > 0:
                time.sleep(wait)
            try:
                response = self.session.smart_connect.orderBook()
            except Exception as exc:  # noqa: BLE001
                log.warning("angelone_order_book_exception", error=str(exc))
                response = None
            finally:
                self._last_order_book_at = time.monotonic()
        if not response or not response.get("status"):
            return None
        return response.get("data") or []

    def get_ltp(self, exchange: str, trading_symbol: str, token: str) -> float:
        response = self.session.smart_connect.ltpData(exchange, trading_symbol, token)
        if not response or not response.get("status"):
            return 0.0
        return float(response.get("data", {}).get("ltp", 0.0))

    def check_margin(self, legs: list[MarginLeg]) -> MarginCheckResult:
        """The real go/no-go gate once mode: live is active - see strategy's Sizing note:
        fixed at 1 lot, this only ever decides affordable-or-not, never scales up."""
        positions = [
            {
                "exchange": leg.exchange,
                "qty": leg.quantity,
                "price": 0,
                "productType": leg.product_type,
                "token": leg.token,
                "tradeType": leg.side.value,
                "orderType": "MARKET",
            }
            for leg in legs
        ]
        response = self.session.smart_connect.getMarginApi({"positions": positions})
        if not response or not response.get("status"):
            return MarginCheckResult(
                required_margin_rs=float("inf"), available_margin_rs=0.0, is_affordable=False,
                raw_response=response or {},
            )

        data = response.get("data", {})
        required = float(data.get("totalMarginRequired", 0.0))

        rms = self.session.smart_connect.rmsLimit()
        available = 0.0
        if rms and rms.get("status"):
            available = float(rms.get("data", {}).get("availablecash", 0.0))

        return MarginCheckResult(required, available, required <= available, raw_response=data)

    def get_positions(self) -> list[PositionSnapshot]:
        response = self.session.smart_connect.position()
        if not response or not response.get("status"):
            raise RuntimeError(f"Angel One positions unavailable: {(response or {}).get('message')}")
        snapshots = []
        for pos in response.get("data") or []:
            qty = int(pos.get("netqty", 0) or 0)
            if qty == 0:
                continue
            snapshots.append(
                PositionSnapshot(
                    exchange=pos.get("exchange", ""),
                    trading_symbol=pos.get("tradingsymbol", ""),
                    token=pos.get("symboltoken", ""),
                    side=OrderSide.BUY if qty > 0 else OrderSide.SELL,
                    quantity=abs(qty),
                    average_price=float(pos.get("avgnetprice", 0.0) or 0.0),
                    ltp=float(pos.get("ltp", 0.0) or 0.0),
                )
            )
        return snapshots
