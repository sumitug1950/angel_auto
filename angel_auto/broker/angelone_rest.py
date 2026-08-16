"""Live Angel One REST broker adapter - real order placement, built to the exact same
BrokerAdapter interface paper/backtest modes use.

CAUTION: place_order() here sends a REAL order to a REAL account when `mode: live`.
This module is intentionally NOT exercised by any automated test against order placement
itself (there's no safe way to "try" placing a real order without risking a real fill) -
it's written to Angel One's documented SmartAPI parameter conventions, and the read-only
paths (margin calculator, order status) were spot-checked live during development. Before
this is ever used in anger: prove the strategy out in paper mode first (per the plan's
"before ever flipping to live" checklist), then test this adapter itself very carefully -
small size, watched closely, on a day you can monitor throughout.
"""
from __future__ import annotations

from angel_auto.broker.angelone_auth import AngelSession
from angel_auto.broker.base import (
    BrokerAdapter,
    MarginCheckResult,
    MarginLeg,
    OrderRequest,
    OrderResult,
    PositionSnapshot,
)
from angel_auto.core.enums import OrderSide, OrderStatus
from angel_auto.logging_conf import get_logger

log = get_logger(__name__)

_ORDER_TYPE_MAP = {"LIMIT": "LIMIT", "MARKET": "MARKET", "SL": "STOPLOSS_LIMIT"}
_STATUS_MAP = {
    "open": OrderStatus.OPEN,
    "open pending": OrderStatus.OPEN,
    "pending": OrderStatus.OPEN,
    "trigger pending": OrderStatus.OPEN,
    "complete": OrderStatus.FILLED,
    "cancelled": OrderStatus.CANCELLED,
    "rejected": OrderStatus.REJECTED,
}


class AngelOneBroker(BrokerAdapter):
    """Real order placement via Angel One SmartAPI. `session` must already be logged in
    (see broker/angelone_auth.py) - this class only ever uses an existing session."""

    def __init__(self, session: AngelSession) -> None:
        self.session = session

    def place_order(self, request: OrderRequest) -> OrderResult:
        params = {
            "variety": request.variety,
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

        try:
            response = self.session.smart_connect.placeOrderFullResponse(params)
        except Exception as exc:  # noqa: BLE001 - a broker/network failure must not crash the OMS
            log.error("angelone_place_order_exception", error=str(exc), symbol=request.trading_symbol)
            return OrderResult("", OrderStatus.REJECTED, message=str(exc))

        if not response or not response.get("status"):
            message = (response or {}).get("message", "unknown error")
            log.error("angelone_place_order_rejected", message=message, symbol=request.trading_symbol)
            return OrderResult("", OrderStatus.REJECTED, message=message)

        order_id = response["data"]["orderid"]
        log.info("angelone_order_placed", order_id=order_id, symbol=request.trading_symbol, side=request.side.value)
        # SmartAPI doesn't return a synchronous fill price/status - it's OPEN until the
        # order book/websocket order-update confirms a fill. The OMS's caller is
        # responsible for polling get_order_status() (Phase 6's PaperBroker fills
        # synchronously, which live can't match exactly - a real gap between modes,
        # documented rather than papered over).
        return OrderResult(order_id, OrderStatus.OPEN)

    def cancel_order(self, broker_order_id: str, variety: str = "NORMAL") -> None:
        response = self.session.smart_connect.cancelOrder(broker_order_id, variety)
        if not response or not response.get("status"):
            log.error(
                "angelone_cancel_order_failed", order_id=broker_order_id, message=(response or {}).get("message")
            )

    def get_order_status(self, broker_order_id: str) -> OrderStatus:
        response = self.session.smart_connect.orderBook()
        if not response or not response.get("status"):
            return OrderStatus.REJECTED
        for order in response.get("data") or []:
            if order.get("orderid") == broker_order_id:
                return _STATUS_MAP.get((order.get("status") or "").lower(), OrderStatus.OPEN)
        return OrderStatus.REJECTED

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
            return []
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
