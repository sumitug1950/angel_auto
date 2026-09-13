"""Tests the AngelOneBroker adapter's request-building and response-parsing logic against
a fake smart_connect object - no real network call, and definitely no real order ever
placed. Verifies the plumbing (param mapping, status mapping, error handling), not Angel
One's actual server behavior.
"""
from angel_auto.broker.angelone_auth import AngelSession
from angel_auto.broker.angelone_rest import AngelOneBroker
from angel_auto.broker.base import MarginLeg, OrderRequest
from angel_auto.core.enums import OrderSide, OrderStatus


class _FakeSmartConnect:
    def __init__(self):
        self.last_place_order_params = None
        self.last_cancel_args = None
        self.place_order_response = {"status": True, "data": {"orderid": "ORDER123"}}
        self.order_book_response = {"status": True, "data": [{"orderid": "ORDER123", "status": "complete"}]}
        self.ltp_response = {"status": True, "data": {"ltp": 300.5}}
        self.margin_response = {"status": True, "data": {"totalMarginRequired": 5000.0}}
        self.rms_response = {"status": True, "data": {"availablecash": 20000.0}}
        self.details_response = {"status": False}
        self.last_details_arg = None
        self.position_response = {
            "status": True,
            "data": [
                {"exchange": "NFO", "tradingsymbol": "NIFTY29SEP2624600CE", "symboltoken": "1",
                 "netqty": "65", "avgnetprice": "300.0", "ltp": "310.0"},
                {"exchange": "NFO", "tradingsymbol": "NIFTY29SEP2625100CE", "symboltoken": "2",
                 "netqty": "0", "avgnetprice": "0", "ltp": "0"},
            ],
        }

    def placeOrderFullResponse(self, params):
        self.last_place_order_params = params
        return self.place_order_response

    def cancelOrder(self, order_id, variety):
        self.last_cancel_args = (order_id, variety)
        return {"status": True}

    def orderBook(self):
        return self.order_book_response

    def individual_order_details(self, unique_order_id):
        self.last_details_arg = unique_order_id
        return self.details_response

    def ltpData(self, exchange, trading_symbol, token):
        return self.ltp_response

    def getMarginApi(self, params):
        self.last_margin_params = params
        return self.margin_response

    def rmsLimit(self):
        return self.rms_response

    def position(self):
        return self.position_response


def _fake_session() -> AngelSession:
    return AngelSession(
        smart_connect=_FakeSmartConnect(), jwt_token="x", refresh_token="y", feed_token="z", client_code="C1",
    )


def test_place_order_builds_correct_params_and_returns_open():
    session = _fake_session()
    broker = AngelOneBroker(session)
    request = OrderRequest(
        exchange="NFO", trading_symbol="NIFTY29SEP2624600CE", token="1", side=OrderSide.BUY,
        quantity=65, order_type="LIMIT", product_type="INTRADAY", price=301.0, tag="leg:1",
    )
    result = broker.place_order(request)

    assert result.status == OrderStatus.OPEN  # SmartAPI doesn't confirm fill synchronously
    assert result.broker_order_id == "ORDER123"
    params = session.smart_connect.last_place_order_params
    assert params["tradingsymbol"] == "NIFTY29SEP2624600CE"
    assert params["transactiontype"] == "BUY"
    assert params["ordertype"] == "LIMIT"
    assert params["quantity"] == "65"
    assert params["price"] == "301.0"


def test_place_order_maps_sl_order_type_and_trigger_price():
    session = _fake_session()
    broker = AngelOneBroker(session)
    request = OrderRequest(
        exchange="NFO", trading_symbol="X", token="1", side=OrderSide.SELL, quantity=65,
        order_type="SL", product_type="INTRADAY", price=250.0, trigger_price=255.0,
    )
    broker.place_order(request)
    params = session.smart_connect.last_place_order_params
    assert params["ordertype"] == "STOPLOSS_LIMIT"
    assert params["triggerprice"] == "255.0"
    assert params["variety"] == "STOPLOSS"  # SmartAPI rejects STOPLOSS_LIMIT under the NORMAL variety


def test_place_order_rejected_response_handled():
    session = _fake_session()
    session.smart_connect.place_order_response = {"status": False, "message": "insufficient margin"}
    broker = AngelOneBroker(session)
    request = OrderRequest(
        exchange="NFO", trading_symbol="X", token="1", side=OrderSide.BUY, quantity=65,
        order_type="MARKET", product_type="INTRADAY",
    )
    result = broker.place_order(request)
    assert result.status == OrderStatus.REJECTED
    assert "insufficient margin" in result.message


def test_place_order_exception_does_not_propagate():
    session = _fake_session()

    def _raise(params):
        raise ConnectionError("network blip")

    session.smart_connect.placeOrderFullResponse = _raise
    broker = AngelOneBroker(session)
    request = OrderRequest(
        exchange="NFO", trading_symbol="X", token="1", side=OrderSide.BUY, quantity=65,
        order_type="MARKET", product_type="INTRADAY",
    )
    result = broker.place_order(request)
    assert result.status == OrderStatus.REJECTED


def test_cancel_order_calls_through_with_correct_args():
    session = _fake_session()
    broker = AngelOneBroker(session)
    broker.cancel_order("ORDER123", variety="NORMAL")
    assert session.smart_connect.last_cancel_args == ("ORDER123", "NORMAL")


def test_get_order_status_maps_complete_to_filled():
    session = _fake_session()
    broker = AngelOneBroker(session)
    assert broker.get_order_status("ORDER123") == OrderStatus.FILLED


def test_order_not_in_book_yet_is_still_working_not_rejected():
    session = _fake_session()
    broker = AngelOneBroker(session)
    assert broker.get_order_status("NOT_FOUND") == OrderStatus.OPEN  # may just not have reached the book yet


def test_get_order_state_reports_partial_fill_details():
    session = _fake_session()
    session.smart_connect.order_book_response = {"status": True, "data": [
        {"orderid": "O9", "orderstatus": "open", "status": "open", "filledshares": "30", "averageprice": "301.25", "text": ""},
    ]}
    state = AngelOneBroker(session).get_order_state("O9")
    assert (state.status, state.filled_quantity, state.average_price) == (OrderStatus.PARTIALLY_FILLED, 30, 301.25)


def test_get_order_state_rejection_carries_broker_reason():
    session = _fake_session()
    session.smart_connect.order_book_response = {"status": True, "data": [
        {"orderid": "O9", "orderstatus": "rejected", "filledshares": "0", "averageprice": "0", "text": "RMS:Margin Exceeds"},
    ]}
    state = AngelOneBroker(session).get_order_state("O9")
    assert state.status == OrderStatus.REJECTED
    assert state.message == "RMS:Margin Exceeds"


def test_place_order_sends_tag_and_uses_order_details_for_status():
    session = _fake_session()
    session.smart_connect.place_order_response = {"status": True, "data": {"orderid": "O7", "uniqueorderid": "U7"}}
    session.smart_connect.details_response = {"status": True, "data": {
        "orderid": "O7", "orderstatus": "complete", "filledshares": "65", "averageprice": 301.5, "text": "",
    }}
    broker = AngelOneBroker(session)
    broker.place_order(OrderRequest(
        exchange="NFO", trading_symbol="X", token="1", side=OrderSide.BUY, quantity=65,
        order_type="LIMIT", product_type="INTRADAY", price=301.0, tag="AA12",
    ))
    assert session.smart_connect.last_place_order_params["ordertag"] == "AA12"

    state = broker.get_order_state("O7")
    assert (state.status, state.filled_quantity, state.average_price) == (OrderStatus.FILLED, 65, 301.5)
    assert session.smart_connect.last_details_arg == "U7"


def test_find_order_by_tag_scans_the_order_book():
    session = _fake_session()
    session.smart_connect.order_book_response = {"status": True, "data": [
        {"orderid": "O1", "ordertag": "AA5", "orderstatus": "cancelled", "filledshares": "0", "averageprice": 0.0},
    ]}
    broker = AngelOneBroker(session, order_book_min_interval_sec=0)
    order_id, state = broker.find_order_by_tag("AA5")
    assert (order_id, state.status) == ("O1", OrderStatus.CANCELLED)
    assert broker.find_order_by_tag("AA6") is None


def test_order_book_failure_is_unknown_not_rejected():
    session = _fake_session()
    session.smart_connect.order_book_response = {"status": False, "message": "rate limit"}
    assert AngelOneBroker(session).get_order_state("ORDER123").status == OrderStatus.OPEN


def test_get_ltp_parses_response():
    session = _fake_session()
    broker = AngelOneBroker(session)
    assert broker.get_ltp("NFO", "X", "1") == 300.5


def test_check_margin_combines_margin_and_rms():
    session = _fake_session()
    broker = AngelOneBroker(session)
    legs = [MarginLeg("NFO", "X", "1", OrderSide.BUY, 65, "INTRADAY")]
    result = broker.check_margin(legs)
    assert result.required_margin_rs == 5000.0
    assert result.available_margin_rs == 20000.0
    assert result.is_affordable is True
    assert session.smart_connect.last_margin_params["positions"][0]["tradeType"] == "BUY"


def test_check_margin_unaffordable_when_required_exceeds_available():
    session = _fake_session()
    session.smart_connect.margin_response = {"status": True, "data": {"totalMarginRequired": 50000.0}}
    broker = AngelOneBroker(session)
    legs = [MarginLeg("NFO", "X", "1", OrderSide.SELL, 65, "INTRADAY")]
    result = broker.check_margin(legs)
    assert result.is_affordable is False


def test_get_positions_filters_zero_quantity_and_maps_side():
    session = _fake_session()
    broker = AngelOneBroker(session)
    positions = broker.get_positions()
    assert len(positions) == 1
    assert positions[0].trading_symbol == "NIFTY29SEP2624600CE"
    assert positions[0].side == OrderSide.BUY
    assert positions[0].quantity == 65
